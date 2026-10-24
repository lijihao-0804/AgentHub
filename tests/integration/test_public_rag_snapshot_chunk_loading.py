from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.control_plane.models import Organization, User, Workspace
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
    RevisionIngestionStatus,
)
from packages.knowledge.retrieval import HybridKnowledgeRetriever

DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL, reason="Set AGENTHUB_TEST_DATABASE_URL for PostgreSQL tests"
    ),
]


class _Components:
    def embed_query(self, _text):
        raise AssertionError("database fixture must not invoke the embedder")

    def encode_query(self, _text):
        raise AssertionError("database fixture must not invoke the sparse encoder")

    def rerank(self, _query, _candidates):
        raise AssertionError("database fixture must not invoke the reranker")

    def dense_search(self, _query, *, scope, limit):
        raise AssertionError("database fixture must not invoke the vector index")

    def sparse_search(self, _query, *, scope, limit):
        raise AssertionError("database fixture must not invoke the vector index")


def _retriever(session: AsyncSession) -> HybridKnowledgeRetriever:
    components = _Components()
    return HybridKnowledgeRetriever(
        session=session,
        dense_embedder=components,
        sparse_encoder=components,
        reranker=components,
        vector_index=components,
    )


async def _workspace(
    session: AsyncSession,
    *,
    organization_name: str,
) -> tuple[UUID, UUID]:
    user = User(
        email=f"{uuid4()}@snapshot-loading.test",
        normalized_email=f"{uuid4()}@snapshot-loading.test",
        password_hash="not-used",
    )
    session.add(user)
    await session.flush()
    organization = Organization(name=organization_name, created_by=user.id)
    session.add(organization)
    await session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=f"workspace-{uuid4()}",
    )
    session.add(workspace)
    await session.flush()
    knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Snapshot loading")
    session.add(knowledge_base)
    await session.flush()
    return workspace.id, knowledge_base.id


async def _document(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    name: str,
    revision_number: int = 1,
    chunk_id: str | None = None,
    text: str | None = None,
) -> tuple[Document, DocumentRevision, DocumentChunk]:
    document = Document(
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        name=name,
    )
    session.add(document)
    await session.flush()
    revision = DocumentRevision(
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        revision_number=revision_number,
        original_filename=name,
        blob_key=f"snapshot-loading/{uuid4().hex}",
        media_type="text/plain",
        file_size=len(name),
        ingestion_status=RevisionIngestionStatus.READY,
    )
    session.add(revision)
    await session.flush()
    chunk = DocumentChunk(
        chunk_id=chunk_id or f"chunk-{uuid4().hex}",
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        document_revision_id=revision.id,
        ordinal=0,
        normalized_content_hash="a" * 64,
        text=text or f"text for {name}",
        locator={"type": "text_range", "char_start": 0, "char_end": len(name)},
    )
    session.add(chunk)
    await session.flush()
    return document, revision, chunk


async def _snapshot(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    documents: tuple[tuple[Document, DocumentRevision], ...],
) -> KnowledgeSnapshot:
    snapshot = KnowledgeSnapshot(
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        content_hash="b" * 64,
    )
    session.add(snapshot)
    await session.flush()
    session.add_all(
        KnowledgeSnapshotItem(
            workspace_id=workspace_id,
            snapshot_id=snapshot.id,
            knowledge_base_id=knowledge_base_id,
            document_id=document.id,
            document_revision_id=revision.id,
        )
        for document, revision in documents
    )
    await session.flush()
    return snapshot


async def _legacy_chunks(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    revision_ids: tuple[str, ...],
    chunk_ids: set[str],
) -> dict[str, DocumentChunk]:
    result = await session.execute(
        select(DocumentChunk)
        .join(
            Document,
            (Document.id == DocumentChunk.document_id)
            & (Document.workspace_id == DocumentChunk.workspace_id)
            & (Document.knowledge_base_id == DocumentChunk.knowledge_base_id),
        )
        .join(
            DocumentRevision,
            (DocumentRevision.id == DocumentChunk.document_revision_id)
            & (DocumentRevision.workspace_id == DocumentChunk.workspace_id)
            & (DocumentRevision.knowledge_base_id == DocumentChunk.knowledge_base_id),
        )
        .where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.knowledge_base_id == knowledge_base_id,
            DocumentChunk.document_revision_id.in_(
                tuple(UUID(revision_id) for revision_id in revision_ids)
            ),
            DocumentChunk.chunk_id.in_(chunk_ids),
        )
    )
    return {chunk.chunk_id: chunk for chunk in result.scalars()}


async def test_snapshot_join_loads_only_candidate_chunks_with_tenant_and_revision_scope() -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_timeout=3)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # The savepoint may commit; the enclosing implicit transaction is
            # rolled back when the session closes, leaving no fixture rows.
            async with session.begin_nested():
                workspace_id, knowledge_base_id = await _workspace(
                    session, organization_name=f"snapshot-loading-{uuid4()}"
                )
                (
                    current_document,
                    current_revision,
                    current_chunk,
                ) = await _document(
                    session,
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    name="current.txt",
                    chunk_id=f"current-{uuid4().hex}",
                )
                (
                    historical_document,
                    historical_revision,
                    historical_chunk,
                ) = await _document(
                    session,
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    name="historical.txt",
                    revision_number=2,
                    chunk_id=f"historical-{uuid4().hex}",
                )
                historical_revision.lifecycle_status = "RETIRED"
                await session.flush()
                (
                    out_of_snapshot_document,
                    out_of_snapshot_revision,
                    out_of_snapshot_chunk,
                ) = await _document(
                    session,
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    name="out-of-snapshot.txt",
                    chunk_id=f"out-of-snapshot-{uuid4().hex}",
                )
                snapshot = await _snapshot(
                    session,
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    documents=(
                        (current_document, current_revision),
                        (historical_document, historical_revision),
                    ),
                )

                other_workspace_id, other_knowledge_base_id = await _workspace(
                    session, organization_name=f"snapshot-loading-other-{uuid4()}"
                )
                (
                    other_document,
                    other_revision,
                    other_chunk,
                ) = await _document(
                    session,
                    workspace_id=other_workspace_id,
                    knowledge_base_id=other_knowledge_base_id,
                    name="cross-workspace.txt",
                    chunk_id=f"cross-workspace-{uuid4().hex}",
                )
                await _snapshot(
                    session,
                    workspace_id=other_workspace_id,
                    knowledge_base_id=other_knowledge_base_id,
                    documents=((other_document, other_revision),),
                )

                revision_ids = tuple(
                    str(revision_id)
                    for revision_id in (
                        current_revision.id,
                        historical_revision.id,
                        out_of_snapshot_revision.id,
                    )
                )
                chunk_ids = {
                    current_chunk.chunk_id,
                    historical_chunk.chunk_id,
                    out_of_snapshot_chunk.chunk_id,
                    other_chunk.chunk_id,
                }
                loaded = await _retriever(session)._load_chunks(
                    session,
                    snapshot_id=snapshot.id,
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    revision_ids=revision_ids,
                    chunk_ids=chunk_ids,
                )
                legacy = await _legacy_chunks(
                    session,
                    workspace_id=workspace_id,
                    knowledge_base_id=knowledge_base_id,
                    revision_ids=tuple(
                        str(revision_id)
                        for revision_id in (current_revision.id, historical_revision.id)
                    ),
                    chunk_ids=chunk_ids,
                )

                assert set(loaded) == {current_chunk.chunk_id, historical_chunk.chunk_id}
                assert loaded[current_chunk.chunk_id][0].text == current_chunk.text
                assert loaded[historical_chunk.chunk_id][2].id == historical_revision.id
                assert loaded[historical_chunk.chunk_id][2].lifecycle_status == "RETIRED"
                assert out_of_snapshot_chunk.chunk_id not in loaded
                assert other_chunk.chunk_id not in loaded
                assert set(loaded) == set(legacy)
                assert {chunk_id: item[0].text for chunk_id, item in loaded.items()} == {
                    chunk_id: item.text for chunk_id, item in legacy.items()
                }
    finally:
        await engine.dispose()
