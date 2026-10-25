"""Real PostgreSQL fixtures and a deterministic compiler for Wiki acceptance tests."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.control_plane.services import TenantService
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential


@dataclass(frozen=True)
class WikiSeed:
    """Persisted identities and contexts shared by one acceptance scenario."""

    owner_user_id: UUID
    viewer_user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    model_profile_id: UUID
    owner_context: WorkspaceExecutionContext
    viewer_context: WorkspaceExecutionContext
    revision_ids: tuple[UUID, UUID]
    document_ids: tuple[UUID, UUID]


async def seed_wiki(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[WorkspaceExecutionContext, UUID, UUID, list[UUID]]:
    """Create two real sources and resolve both owner and viewer access."""

    owner = User(
        email=f"wiki-owner-{uuid4()}@example.test",
        normalized_email=f"wiki-owner-{uuid4()}@example.test",
        password_hash="not-used",
    )
    viewer = User(
        email=f"wiki-viewer-{uuid4()}@example.test",
        normalized_email=f"wiki-viewer-{uuid4()}@example.test",
        password_hash="not-used",
    )
    # The visible and normalized values are independently random above; keep the
    # unique lookup key identical to the visible address for realistic fixtures.
    owner.normalized_email = owner.email
    viewer.normalized_email = viewer.email

    async with factory() as session:
        session.add_all((owner, viewer))
        await session.flush()
        organization = Organization(
            name=f"wiki-acceptance-{uuid4()}",
            created_by=owner.id,
        )
        session.add(organization)
        await session.flush()
        session.add_all(
            (
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=owner.id,
                    role="OWNER",
                ),
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=viewer.id,
                    role="MEMBER",
                ),
            )
        )
        workspace = Workspace(
            organization_id=organization.id,
            name=f"wiki-workspace-{uuid4()}",
        )
        session.add(workspace)
        await session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=viewer.id,
                role="VIEWER",
            )
        )
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Wiki acceptance")
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name="wiki-acceptance-fake",
            secret="fake-wiki-secret",
        )
        session.add_all((knowledge_base, credential))
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="wiki-fake-model",
            temperature=Decimal("0"),
            max_tokens=512,
            timeout_seconds=Decimal("30"),
            capabilities={"max_context_tokens": 8192},
        )
        session.add(profile)
        await session.flush()
        source_specs = (
            (
                "retention-policy.txt",
                "Retention policy: retention period is 7 years. "
                "Records must be retained for audit.",
            ),
            (
                "retention-glossary.txt",
                "Retention glossary: retention period is 5 years. "
                "Records stay in the glossary for reference.",
            ),
        )
        documents: list[Document] = []
        revisions: list[DocumentRevision] = []
        for filename, text in source_specs:
            document = Document(
                workspace_id=workspace.id,
                knowledge_base_id=knowledge_base.id,
                name=filename,
            )
            session.add(document)
            await session.flush()
            revision = DocumentRevision(
                workspace_id=workspace.id,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                revision_number=1,
                original_filename=filename,
                blob_key=f"wiki-acceptance/{uuid4().hex}",
                media_type="text/plain",
                file_size=len(text.encode("utf-8")),
                lifecycle_status=RevisionLifecycleStatus.ACTIVE,
                ingestion_status=RevisionIngestionStatus.READY,
            )
            session.add(revision)
            await session.flush()
            session.add(
                DocumentChunk(
                    chunk_id=f"wiki-seed-{uuid4().hex}",
                    workspace_id=workspace.id,
                    knowledge_base_id=knowledge_base.id,
                    document_id=document.id,
                    document_revision_id=revision.id,
                    ordinal=0,
                    normalized_content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    text=text,
                    locator={
                        "type": "text_range",
                        "char_start": 0,
                        "char_end": len(text),
                    },
                )
            )
            documents.append(document)
            revisions.append(revision)

        owner_context = (
            await TenantService().get_workspace_access(
                session,
                principal=PrincipalContext(
                    request_id=f"wiki-owner-{uuid4()}",
                    trace_id=f"wiki-owner-{uuid4()}",
                    user_id=str(owner.id),
                ),
                workspace_id=workspace.id,
            )
        ).context
        viewer_context = (
            await TenantService().get_workspace_access(
                session,
                principal=PrincipalContext(
                    request_id=f"wiki-viewer-{uuid4()}",
                    trace_id=f"wiki-viewer-{uuid4()}",
                    user_id=str(viewer.id),
                ),
                workspace_id=workspace.id,
            )
        ).context
        await session.commit()

        # Expose the additional real identities through a process-local side
        # table only when tests need them. The public helper signature remains
        # the compact contract requested by the acceptance suite.
        _SEEDS[workspace.id] = WikiSeed(
            owner_user_id=owner.id,
            viewer_user_id=viewer.id,
            organization_id=organization.id,
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            model_profile_id=profile.id,
            owner_context=owner_context,
            viewer_context=viewer_context,
            revision_ids=(revisions[0].id, revisions[1].id),
            document_ids=(documents[0].id, documents[1].id),
        )
        return (
            owner_context,
            knowledge_base.id,
            profile.id,
            [revision.id for revision in revisions],
        )


_SEEDS: dict[UUID, WikiSeed] = {}


def seed_details(workspace_id: UUID | str) -> WikiSeed:
    """Return extra identities from the most recent seed for this workspace."""

    return _SEEDS[UUID(str(workspace_id))]


def viewer_context_for(
    workspace_id: UUID | str,
    *,
    request_id: str | None = None,
) -> WorkspaceExecutionContext:
    """Return the viewer's server-resolved context for API dependency overrides."""

    seed = seed_details(workspace_id)
    if request_id is None:
        return seed.viewer_context
    principal = PrincipalContext(
        request_id=request_id,
        trace_id=request_id,
        user_id=str(seed.viewer_user_id),
    )
    return seed.viewer_context.model_copy(
        update={
            "organization": seed.viewer_context.organization.model_copy(
                update={"principal": principal}
            )
        }
    )


async def add_source(
    factory: async_sessionmaker[AsyncSession],
    context: WorkspaceExecutionContext,
    kb_id: UUID,
    text: str,
    document_id: UUID | None = None,
    revision_number: int = 1,
) -> DocumentRevision:
    """Persist one ready source revision and one exact-text JSONB chunk."""

    workspace_id = UUID(context.workspace_id)
    async with factory() as session:
        document: Document | None = None
        if document_id is None:
            document = Document(
                workspace_id=workspace_id,
                knowledge_base_id=kb_id,
                name=f"wiki-source-{uuid4()}.txt",
            )
            session.add(document)
            await session.flush()
        else:
            document = await session.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.workspace_id == workspace_id,
                    Document.knowledge_base_id == kb_id,
                )
            )
            if document is None:
                raise ValueError("document does not belong to the requested Wiki tenant")
            await session.execute(
                update(DocumentRevision)
                .where(
                    DocumentRevision.workspace_id == workspace_id,
                    DocumentRevision.knowledge_base_id == kb_id,
                    DocumentRevision.document_id == document_id,
                    DocumentRevision.lifecycle_status == RevisionLifecycleStatus.ACTIVE,
                )
                .values(lifecycle_status=RevisionLifecycleStatus.RETIRED)
            )

        revision = DocumentRevision(
            workspace_id=workspace_id,
            knowledge_base_id=kb_id,
            document_id=document.id,
            revision_number=revision_number,
            original_filename=document.name,
            blob_key=f"wiki-acceptance/{uuid4().hex}",
            media_type="text/plain",
            file_size=len(text.encode("utf-8")),
            lifecycle_status=RevisionLifecycleStatus.ACTIVE,
            ingestion_status=RevisionIngestionStatus.READY,
        )
        session.add(revision)
        await session.flush()
        session.add(
            DocumentChunk(
                chunk_id=f"wiki-source-{uuid4().hex}",
                workspace_id=workspace_id,
                knowledge_base_id=kb_id,
                document_id=document.id,
                document_revision_id=revision.id,
                ordinal=0,
                normalized_content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                text=text,
                locator={
                    "type": "text_range",
                    "char_start": 0,
                    "char_end": len(text),
                },
            )
        )
        await session.commit()
        return revision


def _period(text: str) -> str:
    match = re.search(r"retention period is\s+([0-9]+\s+years)", text, re.IGNORECASE)
    if match is not None:
        return match.group(1)
    sentence = next((part.strip() for part in re.split(r"[.!?]", text) if part.strip()), text)
    return sentence[:300]


class FakeCompiler:
    async def review(self, context, claims):
        return [], {"request_count": 0, "cost": "0", "provider": "fake", "model": "fake"}

    """Deterministic, evidence-exact compiler used instead of a model provider."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.chunk_ids: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def extract(
        self,
        context: WorkspaceExecutionContext,
        chunks: list[dict],
    ) -> tuple[dict, dict]:
        del context
        self.calls.append([dict(chunk) for chunk in chunks])
        claims: list[dict[str, Any]] = []
        for chunk in chunks:
            self.chunk_ids.append(chunk["chunk_id"])
            text = chunk["text"]
            claims.extend(
                (
                    {
                        "topic": "Retention",
                        "kind": "concept",
                        "predicate": "period",
                        "value": _period(text),
                        "condition": "",
                        "chunk_id": chunk["chunk_id"],
                        "quote": text,
                        "related_topics": ["Records"],
                    },
                    {
                        "topic": "Records",
                        "kind": "entity",
                        "predicate": "evidence",
                        "value": text[:300],
                        "condition": "",
                        "chunk_id": chunk["chunk_id"],
                        "quote": text,
                        "related_topics": ["Retention"],
                    },
                )
            )
        return (
            {"claims": claims},
            {
                "provider": "fake",
                "model": "wiki-fake-model",
                "request_count": len(chunks),
                "input_tokens": len(chunks) * 20,
                "output_tokens": len(chunks) * 10,
                "total_tokens": len(chunks) * 30,
                "cost": None,
                "currency": None,
                "latency_ms": 0.0,
            },
        )


__all__ = [
    "FakeCompiler",
    "WikiSeed",
    "add_source",
    "seed_details",
    "seed_wiki",
    "viewer_context_for",
]
