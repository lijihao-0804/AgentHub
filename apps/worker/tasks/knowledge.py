from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from apps.worker.celery_app import celery_app
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.knowledge.adapters.celery_queue import CeleryIngestionQueue
from packages.knowledge.blob_store import BlobStoreError, LocalBlobStore
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.ingestion import (
    ClaimedIngestionJob,
    advance_ingestion_stage,
    claim_ingestion_job,
    fail_ingestion_job,
    persist_chunks_and_advance,
    reconcile_ingestion_jobs,
    release_ingestion_for_retry,
    renew_ingestion_lease,
)
from packages.knowledge.models import IngestionStage
from packages.knowledge.parser import DocumentParseError, ProcessDocumentParser

logger = logging.getLogger(__name__)


async def _read_blob(store: LocalBlobStore, blob_key: str) -> bytes:
    content = bytearray()
    async for chunk in store.read(blob_key):
        content.extend(chunk)
    return bytes(content)


async def _renew_lease_loop(
    factory,
    claim: ClaimedIngestionJob,
    settings: Settings,
) -> None:
    while True:
        await asyncio.sleep(settings.knowledge_ingestion_lease_renewal_seconds)
        async with factory() as session:
            renewed = await renew_ingestion_lease(
                session,
                job_id=claim.id,
                lease_token=claim.lease_token,
                lease_seconds=settings.knowledge_ingestion_lease_seconds,
            )
        if not renewed:
            return


async def _cancel_renewal(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _finalize_claim(
    factory,
    claim: ClaimedIngestionJob,
    *,
    terminal_error: tuple[str, str] | None = None,
    retryable_error: tuple[str, str] | None = None,
    settings: Settings,
) -> None:
    async with factory() as session:
        if terminal_error is not None:
            await fail_ingestion_job(
                session,
                claim=claim,
                error_code=terminal_error[0],
                safe_message=terminal_error[1],
            )
        elif retryable_error is not None:
            if claim.attempt_count >= settings.knowledge_ingestion_max_attempts:
                await fail_ingestion_job(
                    session,
                    claim=claim,
                    error_code="MAX_ATTEMPTS_EXCEEDED",
                    safe_message="The ingestion job exceeded its retry limit.",
                )
            else:
                await release_ingestion_for_retry(
                    session,
                    claim=claim,
                    error_code=retryable_error[0],
                    safe_message=retryable_error[1],
                    retry_base_seconds=settings.knowledge_ingestion_retry_base_seconds,
                )


async def _process_knowledge_ingestion(job_id: UUID, settings: Settings) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as session:
            claim = await claim_ingestion_job(
                session,
                job_id=job_id,
                lease_seconds=settings.knowledge_ingestion_lease_seconds,
            )
        if claim is None:
            return

        renewal_task = asyncio.create_task(_renew_lease_loop(factory, claim, settings))
        terminal_error: tuple[str, str] | None = None
        retryable_error: tuple[str, str] | None = None
        try:
            try:
                content = await _read_blob(LocalBlobStore(settings.blob_root), claim.blob_key)
            except BlobStoreError:
                retryable_error = (
                    "BLOB_TEMPORARY_FAILURE",
                    "The source blob could not be read temporarily.",
                )
            else:
                try:
                    parsed = await ProcessDocumentParser(
                        timeout_seconds=settings.knowledge_parser_timeout_seconds,
                        max_pdf_pages=settings.knowledge_max_pdf_pages,
                        max_parsed_chars=settings.knowledge_max_parsed_chars,
                    ).parse(content, media_type=claim.media_type)
                except DocumentParseError as exc:
                    terminal_error = (exc.code, exc.message)
                else:
                    async with factory() as session:
                        transitioned = await advance_ingestion_stage(
                            session,
                            job_id=claim.id,
                            lease_token=claim.lease_token,
                            expected_stage=IngestionStage.PARSING,
                            next_stage=IngestionStage.CHUNKING,
                        )
                    if transitioned:
                        try:
                            chunks = build_deterministic_chunks(
                                claim.document_revision_id,
                                parsed,
                                chunk_size_chars=settings.knowledge_chunk_size_chars,
                                chunk_overlap_chars=settings.knowledge_chunk_overlap_chars,
                            )
                            if not chunks:
                                raise DocumentParseError(
                                    "PARSE_FAILED", "The document contains no usable text."
                                )
                        except DocumentParseError as exc:
                            terminal_error = (exc.code, exc.message)
                        except Exception:
                            terminal_error = (
                                "CHUNKING_FAILED",
                                "The document could not be chunked.",
                            )
                        else:
                            async with factory() as session:
                                persisted = await persist_chunks_and_advance(
                                    session,
                                    claim=claim,
                                    chunks=chunks,
                                )
                            if not persisted:
                                return
                    else:
                        return
        except Exception:
            logger.exception("knowledge_ingestion_worker_error", extra={"job_id": str(job_id)})
            retryable_error = (
                "DATABASE_TEMPORARY_FAILURE",
                "The ingestion worker encountered a temporary database failure.",
            )
        finally:
            await _cancel_renewal(renewal_task)

        await _finalize_claim(
            factory,
            claim,
            terminal_error=terminal_error,
            retryable_error=retryable_error,
            settings=settings,
        )
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True,
    name="agenthub.process_knowledge_ingestion",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_knowledge_ingestion(_task, job_id: str) -> None:
    try:
        parsed_job_id = UUID(job_id)
    except ValueError:
        return
    asyncio.run(_process_knowledge_ingestion(parsed_job_id, get_settings()))


@celery_app.task(
    bind=True,
    name="agenthub.reconcile_knowledge_ingestion",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_knowledge_ingestion(_task) -> None:
    asyncio.run(_reconcile_knowledge_ingestion(get_settings()))


async def _reconcile_knowledge_ingestion(settings: Settings) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as session:
            await reconcile_ingestion_jobs(
                session,
                queue=CeleryIngestionQueue(celery_app),
                settings=settings,
            )
    finally:
        await engine.dispose()


__all__ = [
    "process_knowledge_ingestion",
    "reconcile_knowledge_ingestion",
]
