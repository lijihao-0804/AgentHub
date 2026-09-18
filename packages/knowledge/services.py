from __future__ import annotations

from collections.abc import AsyncIterable
from typing import NoReturn
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.audit import append_audit
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.blob_store import BlobStore, BlobStoreError
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    IngestionJob,
    KnowledgeBase,
)
from packages.knowledge.upload_security import UploadSecurityError


class KnowledgeService:
    async def create_knowledge_base(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        name: str,
    ) -> KnowledgeBase:
        self._require_permission(context, "knowledge_create")
        workspace_id = self._workspace_id(context)
        knowledge_base = KnowledgeBase(workspace_id=workspace_id, name=name.strip())
        session.add(knowledge_base)
        await session.flush()
        append_audit(
            session,
            action="knowledge_base_create",
            resource_type="knowledge_base",
            resource_id=str(knowledge_base.id),
            request_id=context.request_id,
            actor_user_id=self._user_id(context),
            organization_id=self._organization_id(context),
            workspace_id=workspace_id,
            safe_metadata={"outcome": "success"},
        )
        await session.commit()
        return knowledge_base

    async def list_knowledge_bases(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
    ) -> list[KnowledgeBase]:
        workspace_id = self._workspace_id(context)
        result = await session.scalars(
            select(KnowledgeBase)
            .where(KnowledgeBase.workspace_id == workspace_id)
            .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
        )
        return list(result)

    async def upload_document(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        knowledge_base_id: UUID,
        filename: str,
        media_type: str,
        chunks: AsyncIterable[bytes],
        document_id: UUID | None = None,
        blob_store: BlobStore,
    ) -> tuple[Document, DocumentRevision, IngestionJob]:
        self._require_permission(
            context, "knowledge_edit" if document_id is not None else "knowledge_create"
        )
        workspace_id = self._workspace_id(context)
        knowledge_base = await session.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.workspace_id == workspace_id,
            )
        )
        if knowledge_base is None:
            self._not_found()

        document: Document | None = None
        if document_id is not None:
            document = await session.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.workspace_id == workspace_id,
                    Document.knowledge_base_id == knowledge_base_id,
                )
            )
            if document is None:
                self._not_found()
        blob_key = uuid4().hex
        try:
            stored_blob = await blob_store.put(blob_key, chunks)
        except UploadSecurityError as exc:
            raise AgentHubError(exc.code, exc.message, 422) from None
        except BlobStoreError:
            raise AgentHubError(
                "BLOB_WRITE_FAILED", "The uploaded file could not be stored.", 503
            ) from None

        try:
            if document is None:
                document = Document(
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    name=filename,
                )
                session.add(document)
                await session.flush()

            latest_revision = await session.scalar(
                select(DocumentRevision.revision_number)
                .where(DocumentRevision.document_id == document.id)
                .order_by(DocumentRevision.revision_number.desc())
                .limit(1)
            )
            revision = DocumentRevision(
                workspace_id=workspace_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document.id,
                revision_number=(latest_revision or 0) + 1,
                original_filename=filename,
                blob_key=stored_blob.blob_key,
                media_type=media_type,
                file_size=stored_blob.size_bytes,
            )
            session.add(revision)
            await session.flush()
            job = IngestionJob(
                workspace_id=workspace_id,
                knowledge_base_id=knowledge_base_id,
                document_revision_id=revision.id,
            )
            session.add(job)
            append_audit(
                session,
                action="document_revision_upload",
                resource_type="document_revision",
                resource_id=str(revision.id),
                request_id=context.request_id,
                actor_user_id=self._user_id(context),
                organization_id=self._organization_id(context),
                workspace_id=workspace_id,
                safe_metadata={
                    "knowledge_base_id": str(knowledge_base_id),
                    "document_id": str(document.id),
                    "revision_number": revision.revision_number,
                    "file_size": stored_blob.size_bytes,
                },
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            await blob_store.delete(stored_blob.blob_key)
            raise AgentHubError(
                "DOCUMENT_UPLOAD_FAILED", "The document could not be recorded.", 409
            ) from exc
        return document, revision, job

    async def list_revisions(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        knowledge_base_id: UUID,
        document_id: UUID,
    ) -> list[tuple[DocumentRevision, IngestionJob]]:
        workspace_id = self._workspace_id(context)
        result = await session.execute(
            select(DocumentRevision, IngestionJob)
            .join(
                IngestionJob,
                (IngestionJob.workspace_id == DocumentRevision.workspace_id)
                & (IngestionJob.document_revision_id == DocumentRevision.id),
            )
            .where(
                DocumentRevision.workspace_id == workspace_id,
                DocumentRevision.knowledge_base_id == knowledge_base_id,
                DocumentRevision.document_id == document_id,
            )
            .order_by(DocumentRevision.revision_number.desc())
        )
        return list(result.all())

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext, permission: str) -> None:
        if permission not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None

    @staticmethod
    def _organization_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.organization.organization_id)
        except ValueError:
            raise AgentHubError(
                "INVALID_ORGANIZATION", "Organization context is invalid.", 500
            ) from None

    @staticmethod
    def _user_id(context: WorkspaceExecutionContext) -> UUID | None:
        return UUID(context.user_id) if context.user_id is not None else None

    @staticmethod
    def _not_found() -> NoReturn:
        raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)
