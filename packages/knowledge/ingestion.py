"""Reliable ingestion state transitions and reconciliation."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config.settings import Settings
from packages.knowledge.chunking import ChunkCandidate
from packages.knowledge.models import (
    DocumentChunk,
    DocumentRevision,
    IngestionJob,
    IngestionJobStatus,
    IngestionStage,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.queue import IngestionQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedIngestionJob:
    id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    document_revision_id: UUID
    blob_key: str
    media_type: str
    lease_token: str
    stage: str
    attempt_count: int


@dataclass(frozen=True)
class ReconciliationResult:
    requeued_job_ids: tuple[UUID, ...]
    failed_job_ids: tuple[UUID, ...]
    enqueue_failures: tuple[UUID, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def _lease_is_valid(job: IngestionJob, lease_token: str, now: datetime) -> bool:
    return (
        job.status == IngestionJobStatus.PROCESSING
        and job.lease_token == lease_token
        and job.lease_expires_at is not None
        and job.lease_expires_at > now
    )


async def claim_ingestion_job(
    session: AsyncSession,
    *,
    job_id: UUID,
    lease_seconds: int,
) -> ClaimedIngestionJob | None:
    now = _now()
    lease_token = secrets.token_urlsafe(32)[:64]
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    due = or_(IngestionJob.next_attempt_at.is_(None), IngestionJob.next_attempt_at <= now)
    claimable = or_(
        and_(
            IngestionJob.status == IngestionJobStatus.PENDING,
            IngestionJob.stage.in_(tuple(IngestionStage)),
            due,
        ),
        and_(
            IngestionJob.status == IngestionJobStatus.PROCESSING,
            IngestionJob.stage.in_(tuple(IngestionStage)),
            IngestionJob.lease_expires_at.is_not(None),
            IngestionJob.lease_expires_at <= now,
        ),
    )
    result = await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id, claimable)
        .values(
            status=IngestionJobStatus.PROCESSING,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            attempt_count=IngestionJob.attempt_count + 1,
            next_attempt_at=None,
            updated_at=now,
        )
        .returning(IngestionJob.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        return None

    job = await session.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
    if job is None:
        await session.rollback()
        return None
    await session.execute(
        update(DocumentRevision)
        .where(DocumentRevision.id == job.document_revision_id)
        .values(ingestion_status=RevisionIngestionStatus.PROCESSING)
    )
    await session.commit()

    record = await session.execute(
        select(IngestionJob, DocumentRevision)
        .join(DocumentRevision, DocumentRevision.id == IngestionJob.document_revision_id)
        .where(IngestionJob.id == job_id)
    )
    claimed_job, revision = record.one()
    claimed = ClaimedIngestionJob(
        id=claimed_job.id,
        workspace_id=claimed_job.workspace_id,
        knowledge_base_id=claimed_job.knowledge_base_id,
        document_revision_id=claimed_job.document_revision_id,
        blob_key=revision.blob_key,
        media_type=revision.media_type,
        lease_token=claimed_job.lease_token or lease_token,
        stage=claimed_job.stage,
        attempt_count=claimed_job.attempt_count,
    )
    await session.rollback()
    return claimed


async def renew_ingestion_lease(
    session: AsyncSession,
    *,
    job_id: UUID,
    lease_token: str,
    lease_seconds: int,
) -> bool:
    now = _now()
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.status == IngestionJobStatus.PROCESSING,
            IngestionJob.lease_token == lease_token,
            IngestionJob.lease_expires_at > now,
        )
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
    )
    await session.commit()
    return result.rowcount == 1


async def advance_ingestion_stage(
    session: AsyncSession,
    *,
    job_id: UUID,
    lease_token: str,
    expected_stage: str,
    next_stage: str,
) -> bool:
    now = _now()
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.status == IngestionJobStatus.PROCESSING,
            IngestionJob.stage == expected_stage,
            IngestionJob.lease_token == lease_token,
            IngestionJob.lease_expires_at > now,
        )
        .values(stage=next_stage, updated_at=now)
    )
    await session.commit()
    return result.rowcount == 1


async def persist_chunks_and_advance(
    session: AsyncSession,
    *,
    claim: ClaimedIngestionJob,
    chunks: tuple[ChunkCandidate, ...],
) -> bool:
    async with session.begin():
        job = await session.scalar(
            select(IngestionJob)
            .where(IngestionJob.id == claim.id)
            .with_for_update()
        )
        now = _now()
        if job is None or not _lease_is_valid(job, claim.lease_token, now):
            return False
        if job.stage != IngestionStage.CHUNKING:
            return False

        document_id = await _document_id_for_revision(session, claim.document_revision_id)
        rows = [
            {
                "id": uuid4(),
                "chunk_id": chunk.chunk_id,
                "workspace_id": claim.workspace_id,
                "knowledge_base_id": claim.knowledge_base_id,
                "document_id": document_id,
                "document_revision_id": claim.document_revision_id,
                "ordinal": chunk.ordinal,
                "normalized_content_hash": chunk.normalized_content_hash,
                "text": chunk.text,
                "locator": chunk.locator,
            }
            for chunk in chunks
        ]
        if rows:
            chunk_insert = insert(DocumentChunk).values(rows)
            await session.execute(
                chunk_insert.on_conflict_do_update(
                    index_elements=[DocumentChunk.chunk_id],
                    set_={
                        "workspace_id": chunk_insert.excluded.workspace_id,
                        "knowledge_base_id": chunk_insert.excluded.knowledge_base_id,
                        "document_id": chunk_insert.excluded.document_id,
                        "document_revision_id": chunk_insert.excluded.document_revision_id,
                        "ordinal": chunk_insert.excluded.ordinal,
                        "normalized_content_hash": chunk_insert.excluded.normalized_content_hash,
                        "text": chunk_insert.excluded.text,
                        "locator": chunk_insert.excluded.locator,
                    },
                )
            )
        job.stage = IngestionStage.EMBEDDING
        job.updated_at = now
    return True


async def finalize_ingestion_ready(
    session: AsyncSession,
    *,
    job_id: UUID,
    lease_token: str,
) -> bool:
    """Finalize only after the external index has acknowledged the upsert."""

    async with session.begin():
        now = _now()
        job = await session.scalar(
            select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
        )
        if job is None or not _lease_is_valid(job, lease_token, now):
            return False
        if job.stage != IngestionStage.INDEXING:
            return False

        revision = await session.scalar(
            select(DocumentRevision)
            .where(DocumentRevision.id == job.document_revision_id)
            .with_for_update()
        )
        if revision is None:
            return False
        revision.ingestion_status = RevisionIngestionStatus.READY

        revisions = list(
            (
                await session.scalars(
                    select(DocumentRevision)
                    .where(DocumentRevision.document_id == revision.document_id)
                    .order_by(DocumentRevision.revision_number.desc())
                    .with_for_update()
                )
            ).all()
        )
        ready_revisions = [
            item for item in revisions if item.ingestion_status == RevisionIngestionStatus.READY
        ]
        if ready_revisions:
            active_revision = ready_revisions[0]
            for item in ready_revisions:
                item.lifecycle_status = (
                    RevisionLifecycleStatus.ACTIVE
                    if item.id == active_revision.id
                    else RevisionLifecycleStatus.RETIRED
                )

        job.status = IngestionJobStatus.SUCCEEDED
        job.lease_token = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.last_error_code = None
        job.safe_error_message = None
        job.updated_at = now
    return True


async def _document_id_for_revision(session: AsyncSession, revision_id: UUID) -> UUID:
    document_id = await session.scalar(
        select(DocumentRevision.document_id).where(DocumentRevision.id == revision_id)
    )
    if document_id is None:
        raise ValueError("document revision does not exist")
    return document_id


async def release_ingestion_for_retry(
    session: AsyncSession,
    *,
    claim: ClaimedIngestionJob,
    error_code: str,
    safe_message: str,
    retry_base_seconds: int,
) -> bool:
    now = _now()
    delay_seconds = retry_base_seconds * (2 ** max(claim.attempt_count - 1, 0))
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == claim.id,
            IngestionJob.status == IngestionJobStatus.PROCESSING,
            IngestionJob.lease_token == claim.lease_token,
            IngestionJob.lease_expires_at > now,
        )
        .values(
            status=IngestionJobStatus.PENDING,
            lease_token=None,
            lease_expires_at=None,
            next_attempt_at=now + timedelta(seconds=delay_seconds),
            last_error_code=error_code,
            safe_error_message=safe_message,
            updated_at=now,
        )
    )
    await session.commit()
    return result.rowcount == 1


async def fail_ingestion_job(
    session: AsyncSession,
    *,
    claim: ClaimedIngestionJob,
    error_code: str,
    safe_message: str,
) -> bool:
    now = _now()
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == claim.id,
            IngestionJob.status == IngestionJobStatus.PROCESSING,
            IngestionJob.lease_token == claim.lease_token,
            IngestionJob.lease_expires_at > now,
        )
        .values(
            status=IngestionJobStatus.FAILED,
            lease_token=None,
            lease_expires_at=None,
            last_error_code=error_code,
            safe_error_message=safe_message,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    await session.execute(
        update(DocumentRevision)
        .where(DocumentRevision.id == claim.document_revision_id)
        .values(ingestion_status=RevisionIngestionStatus.FAILED)
    )
    await session.commit()
    return True


async def reconcile_ingestion_jobs(
    session: AsyncSession,
    *,
    queue: IngestionQueue,
    settings: Settings,
) -> ReconciliationResult:
    now = _now()
    grace_cutoff = now - timedelta(seconds=settings.knowledge_ingestion_enqueue_grace_seconds)
    due_pending = and_(
        IngestionJob.status == IngestionJobStatus.PENDING,
        IngestionJob.stage.in_(tuple(IngestionStage)),
        IngestionJob.created_at <= grace_cutoff,
        or_(IngestionJob.next_attempt_at.is_(None), IngestionJob.next_attempt_at <= now),
    )
    stale_processing = and_(
        IngestionJob.status == IngestionJobStatus.PROCESSING,
        IngestionJob.stage.in_(tuple(IngestionStage)),
        IngestionJob.lease_expires_at <= now,
    )
    result = await session.scalars(
        select(IngestionJob)
        .where(or_(due_pending, stale_processing))
        .order_by(IngestionJob.created_at, IngestionJob.id)
        .limit(settings.knowledge_reconciliation_batch_size)
        .with_for_update(skip_locked=True)
    )
    jobs = list(result)
    requeued: list[UUID] = []
    failed: list[UUID] = []
    for job in jobs:
        if job.attempt_count >= settings.knowledge_ingestion_max_attempts:
            job.status = IngestionJobStatus.FAILED
            job.lease_token = None
            job.lease_expires_at = None
            job.last_error_code = "MAX_ATTEMPTS_EXCEEDED"
            job.safe_error_message = "The ingestion job exceeded its retry limit."
            job.updated_at = now
            await session.execute(
                update(DocumentRevision)
                .where(DocumentRevision.id == job.document_revision_id)
                .values(ingestion_status=RevisionIngestionStatus.FAILED)
            )
            failed.append(job.id)
            continue
        job.status = IngestionJobStatus.PENDING
        job.lease_token = None
        job.lease_expires_at = None
        job.next_attempt_at = now
        job.updated_at = now
        requeued.append(job.id)
    await session.commit()

    enqueue_failures: list[UUID] = []
    for job_id in requeued:
        try:
            await queue.enqueue(job_id)
        except Exception:
            enqueue_failures.append(job_id)
            logger.warning(
                "knowledge_ingestion_reconciliation_enqueue_failed",
                extra={"job_id": str(job_id)},
            )
    return ReconciliationResult(tuple(requeued), tuple(failed), tuple(enqueue_failures))


__all__ = [
    "ClaimedIngestionJob",
    "ReconciliationResult",
    "advance_ingestion_stage",
    "claim_ingestion_job",
    "fail_ingestion_job",
    "finalize_ingestion_ready",
    "persist_chunks_and_advance",
    "reconcile_ingestion_jobs",
    "release_ingestion_for_retry",
    "renew_ingestion_lease",
]
