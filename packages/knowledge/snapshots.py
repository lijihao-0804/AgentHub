"""Snapshot materialization and runtime resolution for the Knowledge Hub."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.models import (
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)

KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION = 1
LATEST_SELECTOR = "LATEST"


@dataclass(frozen=True, slots=True)
class ResolvedKnowledgeSnapshot:
    """Provider-neutral snapshot identity for runtime consumers."""

    snapshot_id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    content_hash: str
    snapshot_schema_version: int
    item_count: int
    created_at: datetime


def _canonical_membership(
    knowledge_base_id: UUID,
    revisions: list[tuple[UUID, UUID]],
    *,
    schema_version: int = KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION,
) -> dict[str, object]:
    return {
        "snapshot_schema_version": schema_version,
        "knowledge_base_id": str(knowledge_base_id),
        "items": [
            {
                "document_id": str(document_id),
                "document_revision_id": str(revision_id),
            }
            for document_id, revision_id in revisions
        ],
    }


def _content_hash(
    knowledge_base_id: UUID,
    revisions: list[tuple[UUID, UUID]],
    *,
    schema_version: int = KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION,
) -> str:
    return canonical_json_hash(
        _canonical_membership(knowledge_base_id, revisions, schema_version=schema_version)
    )


class KnowledgeSnapshotService:
    async def create_current_snapshot(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        knowledge_base_id: UUID,
    ) -> ResolvedKnowledgeSnapshot:
        self._require_permission(context)
        workspace_id = self._workspace_id(context)
        await self._require_knowledge_base(session, workspace_id, knowledge_base_id)
        revisions = await self._current_revisions(session, workspace_id, knowledge_base_id)
        membership = [(revision.document_id, revision.id) for revision in revisions]
        content_hash = _content_hash(knowledge_base_id, membership)

        existing = await session.scalar(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.workspace_id == workspace_id,
                KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
                KnowledgeSnapshot.content_hash == content_hash,
            )
        )
        if existing is not None:
            return await self._resolved(session, existing, expected_membership=membership)

        snapshot = KnowledgeSnapshot(
            workspace_id=workspace_id,
            knowledge_base_id=knowledge_base_id,
            content_hash=content_hash,
            snapshot_schema_version=KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION,
        )
        session.add(snapshot)
        try:
            await session.flush()
            session.add_all(
                [
                    KnowledgeSnapshotItem(
                        workspace_id=workspace_id,
                        snapshot_id=snapshot.id,
                        knowledge_base_id=knowledge_base_id,
                        document_id=document_id,
                        document_revision_id=revision_id,
                    )
                    for document_id, revision_id in membership
                ]
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            existing = await session.scalar(
                select(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.workspace_id == workspace_id,
                    KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
                    KnowledgeSnapshot.content_hash == content_hash,
                )
            )
            if existing is None:
                raise AgentHubError(
                    "SNAPSHOT_MATERIALIZATION_FAILED",
                    "The knowledge snapshot could not be materialized.",
                    409,
                ) from exc
            return await self._resolved(session, existing, expected_membership=membership)

        return await self._resolved(session, snapshot, expected_membership=membership)

    async def resolve_snapshot(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        knowledge_base_id: UUID,
        selector: UUID | str,
    ) -> ResolvedKnowledgeSnapshot:
        self._require_permission(context)
        if isinstance(selector, str) and selector.upper() == LATEST_SELECTOR:
            return await self.create_current_snapshot(session, context, knowledge_base_id)
        try:
            snapshot_id = selector if isinstance(selector, UUID) else UUID(str(selector))
        except (AttributeError, ValueError):
            raise AgentHubError(
                "INVALID_SNAPSHOT_SELECTOR",
                "The knowledge snapshot selector is invalid.",
                400,
            ) from None

        workspace_id = self._workspace_id(context)
        snapshot = await session.scalar(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.id == snapshot_id,
                KnowledgeSnapshot.workspace_id == workspace_id,
                KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
            )
        )
        if snapshot is None:
            raise AgentHubError("SNAPSHOT_NOT_FOUND", "The knowledge snapshot was not found.", 404)
        return await self._resolved(session, snapshot)

    async def _current_revisions(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        knowledge_base_id: UUID,
    ) -> list[DocumentRevision]:
        result = await session.scalars(
            select(DocumentRevision)
            .where(
                DocumentRevision.workspace_id == workspace_id,
                DocumentRevision.knowledge_base_id == knowledge_base_id,
                DocumentRevision.ingestion_status == RevisionIngestionStatus.READY,
                DocumentRevision.lifecycle_status == RevisionLifecycleStatus.ACTIVE,
            )
            .order_by(
                DocumentRevision.document_id,
                DocumentRevision.revision_number,
                DocumentRevision.id,
            )
        )
        revisions = list(result)
        seen_documents: set[UUID] = set()
        for revision in revisions:
            if revision.document_id in seen_documents:
                raise AgentHubError(
                    "KNOWLEDGE_REVISION_STATE_CONFLICT",
                    "The knowledge base has conflicting current revisions.",
                    409,
                )
            seen_documents.add(revision.document_id)
        return revisions

    async def _resolved(
        self,
        session: AsyncSession,
        snapshot: KnowledgeSnapshot,
        *,
        expected_membership: list[tuple[UUID, UUID]] | None = None,
    ) -> ResolvedKnowledgeSnapshot:
        result = await session.execute(
            select(KnowledgeSnapshotItem.document_id, KnowledgeSnapshotItem.document_revision_id)
            .where(
                KnowledgeSnapshotItem.workspace_id == snapshot.workspace_id,
                KnowledgeSnapshotItem.knowledge_base_id == snapshot.knowledge_base_id,
                KnowledgeSnapshotItem.snapshot_id == snapshot.id,
            )
            .order_by(
                KnowledgeSnapshotItem.document_id,
                KnowledgeSnapshotItem.document_revision_id,
            )
        )
        membership = [(document_id, revision_id) for document_id, revision_id in result]
        if expected_membership is not None and membership != expected_membership:
            raise AgentHubError(
                "KNOWLEDGE_SNAPSHOT_INCONSISTENT",
                "The knowledge snapshot membership is inconsistent.",
                500,
            )
        if (
            _content_hash(
                snapshot.knowledge_base_id,
                membership,
                schema_version=snapshot.snapshot_schema_version,
            )
            != snapshot.content_hash
        ):
            raise AgentHubError(
                "KNOWLEDGE_SNAPSHOT_INCONSISTENT",
                "The knowledge snapshot content hash is inconsistent.",
                500,
            )
        return ResolvedKnowledgeSnapshot(
            snapshot_id=snapshot.id,
            workspace_id=snapshot.workspace_id,
            knowledge_base_id=snapshot.knowledge_base_id,
            content_hash=snapshot.content_hash,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            item_count=len(membership),
            created_at=snapshot.created_at,
        )

    @staticmethod
    async def _require_knowledge_base(
        session: AsyncSession,
        workspace_id: UUID,
        knowledge_base_id: UUID,
    ) -> None:
        exists = await session.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.workspace_id == workspace_id,
            )
        )
        if exists is None:
            raise AgentHubError(
                "KNOWLEDGE_BASE_NOT_FOUND",
                "The knowledge base was not found.",
                404,
            )

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext) -> None:
        if "knowledge_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


__all__ = [
    "KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION",
    "LATEST_SELECTOR",
    "KnowledgeSnapshotService",
    "ResolvedKnowledgeSnapshot",
]
