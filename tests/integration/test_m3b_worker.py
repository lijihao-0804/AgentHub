from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.celery_app import create_celery_app
from packages.control_plane.models import Organization, User, Workspace
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.adapters.celery_queue import CeleryIngestionQueue
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.ingestion import (
    advance_ingestion_stage,
    claim_ingestion_job,
    reconcile_ingestion_jobs,
)
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    IngestionJob,
    IngestionJobStatus,
    IngestionStage,
    KnowledgeBase,
)
from packages.knowledge.services import KnowledgeService

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
TEST_REDIS_URL = os.environ.get("AGENTHUB_TEST_REDIS_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not TEST_REDIS_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL and AGENTHUB_TEST_REDIS_URL for M3-B integration.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


async def _bytes(value: bytes) -> AsyncIterator[bytes]:
    yield value


class FailingQueue:
    async def enqueue(self, job_id: UUID) -> None:
        del job_id
        raise RuntimeError("simulated broker outage")


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_factory(
    migrated_database: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


def test_settings_keep_real_redis_boundary() -> None:
    assert TEST_REDIS_URL.startswith("redis://")


async def _create_pending_job(
    factory: async_sessionmaker[AsyncSession],
    blob_root: Path,
) -> tuple[UUID, UUID]:
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@example.test",
            normalized_email=f"{uuid4()}@example.test",
            password_hash="not-used-in-this-test",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M3-B {uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        workspace = Workspace(organization_id=organization.id, name=f"Workspace {uuid4()}")
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Worker test")
        session.add(knowledge_base)
        await session.flush()
        document = Document(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            name="worker.txt",
        )
        session.add(document)
        await session.flush()
        blob_key = f"m3b/{uuid4().hex}.txt"
        revision = DocumentRevision(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            revision_number=1,
            original_filename="worker.txt",
            blob_key=blob_key,
            media_type="text/plain",
            file_size=32,
        )
        session.add(revision)
        await session.flush()
        job = IngestionJob(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_revision_id=revision.id,
            status=IngestionJobStatus.PENDING,
            stage=IngestionStage.PARSING,
        )
        session.add(job)
        await session.commit()
        job_id = job.id
    await LocalBlobStore(blob_root).put(
        blob_key,
        _bytes(b"AgentHub M3-B deterministic worker content."),
    )
    return job_id, revision.id


async def _create_knowledge_context(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[WorkspaceExecutionContext, UUID]:
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@example.test",
            normalized_email=f"{uuid4()}@example.test",
            password_hash="not-used-in-this-test",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M3-B upload {uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        workspace = Workspace(organization_id=organization.id, name=f"Workspace {uuid4()}")
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Upload test")
        session.add(knowledge_base)
        await session.commit()
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="m3b-upload",
                trace_id="m3b-upload",
                user_id=str(user.id),
            ),
            organization_id=str(organization.id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace.id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"knowledge_create"}),
    )
    return context, knowledge_base.id


async def _wait_for_embedding(
    factory: async_sessionmaker[AsyncSession], job_id: UUID, timeout: float = 20
) -> IngestionJob:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            job = await session.get(IngestionJob, job_id)
            if job is not None and job.stage == IngestionStage.EMBEDDING:
                return job
            if job is not None and job.status == IngestionJobStatus.FAILED:
                pytest.fail(f"worker failed: {job.last_error_code} {job.safe_error_message}")
        await asyncio.sleep(0.25)
    pytest.fail("Celery worker did not finish M3-B parsing/chunking in time")


@pytest.mark.asyncio
async def test_real_celery_worker_is_idempotent_and_stops_at_embedding(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    blob_root = Path(os.environ.get("AGENTHUB_BLOB_ROOT", "data/blobs")).resolve()
    get_settings.cache_clear()
    settings = Settings(
        database_url=async_database_url(TEST_DATABASE_URL),
        redis_url=TEST_REDIS_URL,
        blob_root=str(blob_root),
        knowledge_ingestion_enqueue_grace_seconds=0,
    )
    job_id, revision_id = await _create_pending_job(db_factory, blob_root)
    queue = CeleryIngestionQueue(create_celery_app(settings))

    await queue.enqueue(job_id)
    await _wait_for_embedding(db_factory, job_id)
    await queue.enqueue(job_id)
    await asyncio.sleep(1)

    async with db_factory() as session:
        job = await session.get(IngestionJob, job_id)
        chunks = list(
            (
                await session.scalars(
                    select(DocumentChunk).where(DocumentChunk.document_revision_id == revision_id)
                )
            ).all()
        )
    assert job is not None
    assert job.status == IngestionJobStatus.PROCESSING
    assert job.stage == IngestionStage.EMBEDDING
    assert len(chunks) == 1


@pytest.mark.asyncio
async def test_expired_worker_lease_cannot_advance_after_takeover(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    job_id, _ = await _create_pending_job(db_factory, tmp_path / "blobs")
    async with db_factory() as session:
        first = await claim_ingestion_job(session, job_id=job_id, lease_seconds=5)
    assert first is not None

    async with db_factory() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
        second = await claim_ingestion_job(session, job_id=job_id, lease_seconds=5)
    assert second is not None
    assert second.lease_token != first.lease_token

    async with db_factory() as session:
        advanced = await advance_ingestion_stage(
            session,
            job_id=job_id,
            lease_token=first.lease_token,
            expected_stage=IngestionStage.PARSING,
            next_stage=IngestionStage.CHUNKING,
        )
    assert advanced is False

    async with db_factory() as session:
        job = await session.get(IngestionJob, job_id)
    assert job is not None
    assert job.stage == IngestionStage.PARSING


@pytest.mark.asyncio
async def test_enqueue_loss_keeps_pending_and_reconciliation_requeues_real_job(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    blob_root = Path(os.environ.get("AGENTHUB_BLOB_ROOT", "data/blobs")).resolve()
    context, knowledge_base_id = await _create_knowledge_context(db_factory)
    settings = Settings(
        database_url=async_database_url(TEST_DATABASE_URL),
        redis_url=TEST_REDIS_URL,
        blob_root=str(blob_root),
        knowledge_ingestion_enqueue_grace_seconds=0,
    )
    async with db_factory() as session:
        _, revision, job = await KnowledgeService().upload_document(
            session,
            context=context,
            knowledge_base_id=knowledge_base_id,
            filename="lost-enqueue.txt",
            media_type="text/plain",
            chunks=_bytes(b"reconciliation content"),
            blob_store=LocalBlobStore(blob_root),
            queue=FailingQueue(),
        )
    assert revision.ingestion_status == "PENDING"
    assert job.status == IngestionJobStatus.PENDING

    async with db_factory() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job.id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    async with db_factory() as session:
        result = await reconcile_ingestion_jobs(
            session,
            queue=CeleryIngestionQueue(create_celery_app(settings)),
            settings=settings,
        )
    assert job.id in result.requeued_job_ids
    await _wait_for_embedding(db_factory, job.id)

    async with db_factory() as session:
        chunks = list(
            (
                await session.scalars(
                    select(DocumentChunk).where(DocumentChunk.document_revision_id == revision.id)
                )
            ).all()
        )
    assert len(chunks) == 1
