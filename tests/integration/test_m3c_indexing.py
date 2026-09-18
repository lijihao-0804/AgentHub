from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from packages.control_plane.models import Organization, User, Workspace
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.ingestion import claim_ingestion_job, finalize_ingestion_ready
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    IngestionJob,
    IngestionJobStatus,
    IngestionStage,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
TEST_QDRANT_URL = os.environ.get("AGENTHUB_TEST_QDRANT_URL", "http://127.0.0.1:6333").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not TEST_QDRANT_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL and AGENTHUB_TEST_QDRANT_URL for M3-C integration.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous


@pytest_asyncio.fixture
async def db_factory(
    migrated_database: None,
) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_revision(
    factory: async_sessionmaker[AsyncSession],
    *,
    blob_root: Path,
    revision_number: int = 1,
    stage: IngestionStage = IngestionStage.PARSING,
    status: IngestionJobStatus = IngestionJobStatus.PENDING,
    with_chunk: bool = False,
) -> tuple[UUID, UUID, UUID]:
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@m3c.test",
            normalized_email=f"{uuid4()}@m3c.test",
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M3-C {uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        workspace = Workspace(organization_id=organization.id, name=f"workspace-{uuid4()}")
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="M3-C test")
        session.add(knowledge_base)
        await session.flush()
        document = Document(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            name="m3c.txt",
        )
        session.add(document)
        await session.flush()
        blob_key = f"m3c/{uuid4().hex}.txt"
        revision = DocumentRevision(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            revision_number=revision_number,
            original_filename="m3c.txt",
            blob_key=blob_key,
            media_type="text/plain",
            file_size=32,
            ingestion_status=(
                RevisionIngestionStatus.PROCESSING
                if stage == IngestionStage.INDEXING
                else RevisionIngestionStatus.PENDING
            ),
        )
        session.add(revision)
        await session.flush()
        if with_chunk:
            session.add(
                DocumentChunk(
                    chunk_id=f"m3c-{uuid4().hex}",
                    workspace_id=workspace.id,
                    knowledge_base_id=knowledge_base.id,
                    document_id=document.id,
                    document_revision_id=revision.id,
                    ordinal=0,
                    normalized_content_hash="a" * 64,
                    text="中文 knowledge and English retrieval",
                    locator={"type": "text_range", "char_start": 0, "char_end": 35},
                )
            )
        job = IngestionJob(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_revision_id=revision.id,
            status=status,
            stage=stage,
        )
        session.add(job)
        await session.commit()
    if stage == IngestionStage.PARSING:
        await LocalBlobStore(blob_root).put(
            blob_key,
            _bytes("中文 knowledge and English retrieval".encode()),
        )
    return job.id, revision.id, document.id


async def _bytes(value: bytes):
    yield value


def _settings(collection: str, blob_root: Path) -> Settings:
    return Settings(
        testing=True,
        database_url=async_database_url(TEST_DATABASE_URL),
        qdrant_url=TEST_QDRANT_URL,
        knowledge_qdrant_collection=collection,
        knowledge_dense_vector_size=16,
        blob_root=str(blob_root),
        knowledge_ingestion_enqueue_grace_seconds=0,
    )


@pytest.mark.asyncio
async def test_real_postgres_qdrant_indexing_is_ready_and_retryable(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    blob_root = Path("data/blobs/m3c-integration") / uuid4().hex
    collection = f"agenthub_m3c_{uuid4().hex}"
    settings = _settings(collection, blob_root)
    job_id, revision_id, _ = await _seed_revision(db_factory, blob_root=blob_root)

    await _process_knowledge_ingestion(job_id, settings)
    async with db_factory() as session:
        revision = await session.get(DocumentRevision, revision_id)
        job = await session.get(IngestionJob, job_id)
    assert revision is not None and revision.ingestion_status == RevisionIngestionStatus.READY
    assert job is not None and job.status == IngestionJobStatus.SUCCEEDED

    # A pending INDEXING job models a crash after Qdrant acknowledged the upsert
    # but before PostgreSQL READY finalization. Recompute + deterministic upsert is safe.
    retry_job, retry_revision, _ = await _seed_revision(
        db_factory,
        blob_root=blob_root,
        stage=IngestionStage.INDEXING,
        with_chunk=True,
    )
    await _process_knowledge_ingestion(retry_job, settings)
    async with db_factory() as session:
        retry_revision_record = await session.get(DocumentRevision, retry_revision)
        retry_job_record = await session.get(IngestionJob, retry_job)
    assert retry_revision_record is not None
    assert retry_revision_record.ingestion_status == RevisionIngestionStatus.READY
    assert retry_job_record is not None
    assert retry_job_record.status == IngestionJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_ready_revision_replacement_is_ordered_by_revision_number(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    blob_root = Path("data/blobs/m3c-integration") / uuid4().hex
    first_job, first_revision, document_id = await _seed_revision(
        db_factory,
        blob_root=blob_root,
        revision_number=1,
        stage=IngestionStage.INDEXING,
    )
    async with db_factory() as session:
        first_revision_record = await session.get(DocumentRevision, first_revision)
        assert first_revision_record is not None
        second_revision_record = DocumentRevision(
            workspace_id=first_revision_record.workspace_id,
            knowledge_base_id=first_revision_record.knowledge_base_id,
            document_id=document_id,
            revision_number=2,
            original_filename="m3c.txt",
            blob_key=f"m3c/{uuid4().hex}.txt",
            media_type="text/plain",
            file_size=1,
            ingestion_status=RevisionIngestionStatus.PROCESSING,
        )
        session.add(second_revision_record)
        await session.flush()
        second_job = IngestionJob(
            workspace_id=second_revision_record.workspace_id,
            knowledge_base_id=second_revision_record.knowledge_base_id,
            document_revision_id=second_revision_record.id,
            status=IngestionJobStatus.PENDING,
            stage=IngestionStage.INDEXING,
        )
        session.add(second_job)
        await session.commit()
        second_job_id = second_job.id

    async with db_factory() as session:
        second_claim = await claim_ingestion_job(
            session, job_id=second_job_id, lease_seconds=60
        )
    assert second_claim is not None
    async with db_factory() as session:
        assert await finalize_ingestion_ready(
            session,
            job_id=second_job_id,
            lease_token=second_claim.lease_token,
        )

    async with db_factory() as session:
        first_claim = await claim_ingestion_job(session, job_id=first_job, lease_seconds=60)
    assert first_claim is not None
    async with db_factory() as session:
        assert await finalize_ingestion_ready(
            session,
            job_id=first_job,
            lease_token=first_claim.lease_token,
        )

    async with db_factory() as session:
        first = await session.get(DocumentRevision, first_revision)
        second = await session.get(DocumentRevision, second_revision_record.id)
    assert first is not None and first.lifecycle_status == RevisionLifecycleStatus.RETIRED
    assert second is not None and second.lifecycle_status == RevisionLifecycleStatus.ACTIVE
