"""PostgreSQL replay and pinned-snapshot checks with an in-process Qdrant and fake vectors."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select, update

from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from packages.core.config.settings import Settings
from packages.knowledge.adapters.fakes import (
    DeterministicFakeDenseEmbedder,
    DeterministicFakeReranker,
    DeterministicFakeSparseEncoder,
)
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.chunking_policy import ChunkingPolicy, ChunkingStrategy
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.ingestion import advance_ingestion_stage, claim_ingestion_job
from packages.knowledge.models import DocumentChunk, DocumentRevision, IngestionJob, IngestionStage
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.services import KnowledgeService
from packages.knowledge.snapshots import KnowledgeSnapshotService
from tests.integration.test_m3c_indexing import _seed_revision
from tests.integration.test_m3f_snapshot import (
    TEST_DATABASE_URL,
    _context,
    _seed_bundle,
    async_database_url,
)
from tests.integration.test_m3f_snapshot import db_factory as db_factory
from tests.integration.test_m3f_snapshot import migrated_database as migrated_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Set AGENTHUB_TEST_DATABASE_URL"),
]


async def _expire(factory, job_id):
    async with factory() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_legacy_pending_window_freezes_once_and_indexing_never_invents_a_policy(
    db_factory, tmp_path
):
    settings = Settings(knowledge_chunk_size_chars=90, knowledge_chunk_overlap_chars=12)
    job_id, revision_id, _ = await _seed_revision(db_factory, blob_root=tmp_path)
    async with db_factory() as session:
        first = await claim_ingestion_job(
            session, job_id=job_id, lease_seconds=60, settings=settings
        )
    await _expire(db_factory, job_id)
    settings.knowledge_chunk_size_chars = 220
    async with db_factory() as session:
        second = await claim_ingestion_job(
            session, job_id=job_id, lease_seconds=60, settings=settings
        )
        row = await session.get(DocumentRevision, revision_id)
        assert row.chunk_size_chars == 90
    assert first.lease_token != second.lease_token
    assert (
        first.chunking_policy
        == second.chunking_policy
        == ChunkingPolicy(ChunkingStrategy.FIXED, 90, 12)
    )
    indexed_job, indexed_revision, _ = await _seed_revision(
        db_factory, blob_root=tmp_path, stage=IngestionStage.INDEXING, with_chunk=True
    )
    async with db_factory() as session:
        indexed = await claim_ingestion_job(
            session, job_id=indexed_job, lease_seconds=60, settings=settings
        )
        historical = await session.get(DocumentRevision, indexed_revision)
        assert indexed.chunking_policy is None and historical.chunking_strategy is None


@pytest.mark.asyncio
async def test_revision_override_survives_takeover_redelivery_and_pinned_history(
    db_factory, tmp_path
):
    bundle = await _seed_bundle(db_factory)
    context = _context(bundle).model_copy(
        update={"permissions": frozenset({"knowledge_run", "knowledge_edit"})}
    )
    snapshots = KnowledgeSnapshotService()
    async with db_factory() as session:
        session.add(
            DocumentChunk(
                workspace_id=bundle.workspace_id,
                knowledge_base_id=bundle.knowledge_base_id,
                document_id=bundle.document_id,
                document_revision_id=bundle.revision_id,
                chunk_id=f"old-{bundle.revision_id}",
                ordinal=0,
                normalized_content_hash="a" * 64,
                text="Retained old wording.",
                locator={"type": "page", "page": 4},
            )
        )
        await session.commit()
        old = await snapshots.create_current_snapshot(session, context, bundle.knowledge_base_id)
        old_preview = await snapshots.preview_chunks(
            session, context, bundle.knowledge_base_id, old.snapshot_id
        )

    source = (
        "# Rules\n"
        + "完整的值在这里。" * 14
        + "\n# Fees\n| Plan | Price |\n|---|---|\n| Alpha | EUR 12 |"
    )

    async def upload():
        yield source.encode()

    settings = Settings(
        database_url=async_database_url(TEST_DATABASE_URL),
        blob_root=str(tmp_path),
        knowledge_chunk_size_chars=100,
        knowledge_chunk_overlap_chars=17,
    )
    async with db_factory() as session:
        _, revision, job = await KnowledgeService().upload_document(
            session,
            context=context,
            knowledge_base_id=bundle.knowledge_base_id,
            document_id=bundle.document_id,
            filename="rules.md",
            media_type="text/markdown",
            chunks=upload(),
            blob_store=LocalBlobStore(tmp_path),
            queue=AsyncMock(),
            chunking_strategy=ChunkingStrategy.ADAPTIVE,
            settings=settings,
        )
    assert (
        revision.chunking_strategy,
        revision.chunk_size_chars,
        revision.chunk_overlap_chars,
    ) == ("adaptive_v1", 75, 0)
    settings.knowledge_chunk_size_chars = 275
    async with db_factory() as session:
        claim = await claim_ingestion_job(
            session, job_id=job.id, lease_seconds=60, settings=settings
        )
        assert await advance_ingestion_stage(
            session,
            job_id=job.id,
            lease_token=claim.lease_token,
            expected_stage=IngestionStage.PARSING,
            next_stage=IngestionStage.CHUNKING,
        )
    expected = claim.chunking_policy.build(
        revision.id,
        ParsedDocument(
            (ParsedBlock(source, {"type": "text_range", "char_start": 0, "char_end": len(source)}),)
        ),
        media_type="text/markdown",
    )
    await _expire(db_factory, job.id)
    client = QdrantClient(":memory:")
    index = QdrantVectorIndex(
        url="unused", collection_name="chunks", dense_vector_size=8, client=client
    )
    components = RetrievalComponents(
        DeterministicFakeDenseEmbedder(dimension=8),
        DeterministicFakeSparseEncoder(),
        DeterministicFakeReranker(),
        index,
    )
    try:
        for _ in range(2):
            await _process_knowledge_ingestion(
                job.id, settings, indexing_factory=lambda _: components
            )
        assert client.count("chunks", exact=True).count == len(expected)
    finally:
        client.close()
    async with db_factory() as session:
        chunks = list(
            await session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_revision_id == revision.id)
                .order_by(DocumentChunk.ordinal)
            )
        )
        assert [(c.chunk_id, c.text, c.locator) for c in chunks] == [
            (c.chunk_id, c.text, c.locator) for c in expected
        ]
        assert (await session.get(IngestionJob, job.id)).attempt_count == 2
        new = await snapshots.create_current_snapshot(session, context, bundle.knowledge_base_id)
        assert new.content_hash != old.content_hash
        assert (
            await snapshots.preview_chunks(
                session, context, bundle.knowledge_base_id, old.snapshot_id
            )
            == old_preview
        )
        assert (await session.get(DocumentRevision, bundle.revision_id)).chunk_size_chars is None
