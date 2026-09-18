from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from apps.worker.celery_app import celery_app
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.knowledge.adapters.celery_queue import CeleryIngestionQueue
from packages.knowledge.blob_store import BlobStoreError, LocalBlobStore
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.composition import (
    RetrievalComponents,
    RetrievalComponentsFactory,
    production_retrieval_components,
)
from packages.knowledge.contracts import (
    DenseEmbedder,
    KnowledgeProviderError,
    SparseEncoder,
    VectorIndex,
    VectorRecord,
    provider_error_is_retryable,
)
from packages.knowledge.ingestion import (
    ClaimedIngestionJob,
    advance_ingestion_stage,
    claim_ingestion_job,
    fail_ingestion_job,
    finalize_ingestion_ready,
    persist_chunks_and_advance,
    reconcile_ingestion_jobs,
    release_ingestion_for_retry,
    renew_ingestion_lease,
)
from packages.knowledge.models import DocumentChunk, IngestionStage
from packages.knowledge.parser import DocumentParseError, ProcessDocumentParser
from packages.knowledge.point_ids import deterministic_point_id
from packages.observability import NoopTraceSink
from packages.observability.contracts import TraceSink, TraceSpan

logger = logging.getLogger(__name__)


IndexingComponents = RetrievalComponents
IndexingComponentsFactory = RetrievalComponentsFactory


@dataclass(frozen=True)
class _IndexingOutcome:
    terminal_error: tuple[str, str] | None
    retryable_error: tuple[str, str] | None
    stale: bool
    chunk_count: int
    point_count: int


async def _safe_trace_start(
    sink: TraceSink,
    name: str,
    attributes: Mapping[str, object],
) -> TraceSpan | None:
    try:
        return await sink.start_span(name, attributes)
    except Exception:
        logger.warning("knowledge_trace_start_failed", extra={"span": name})
        return None


async def _safe_trace_end(
    span: TraceSpan | None,
    *,
    attributes: Mapping[str, object],
    status: str,
    failure_code: str | None,
) -> None:
    if span is None:
        return
    try:
        await span.end(attributes=attributes, status=status, failure_code=failure_code)
    except Exception:
        logger.warning("knowledge_trace_end_failed")


async def _read_blob(store: LocalBlobStore, blob_key: str) -> bytes:
    content = bytearray()
    async for chunk in store.read(blob_key):
        content.extend(chunk)
    return bytes(content)


async def _renew_lease_loop(factory, claim: ClaimedIngestionJob, settings: Settings) -> None:
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
) -> str:
    async with factory() as session:
        if terminal_error is not None:
            finalized = await fail_ingestion_job(
                session,
                claim=claim,
                error_code=terminal_error[0],
                safe_message=terminal_error[1],
            )
            return "failed" if finalized else "stale"
        elif retryable_error is not None:
            if claim.attempt_count >= settings.knowledge_ingestion_max_attempts:
                finalized = await fail_ingestion_job(
                    session,
                    claim=claim,
                    error_code="MAX_ATTEMPTS_EXCEEDED",
                    safe_message="The ingestion job exceeded its retry limit.",
                )
                return "failed" if finalized else "stale"
            else:
                released = await release_ingestion_for_retry(
                    session,
                    claim=claim,
                    error_code=retryable_error[0],
                    safe_message=retryable_error[1],
                    retry_base_seconds=settings.knowledge_ingestion_retry_base_seconds,
                )
                return "retrying" if released else "stale"
    return "succeeded"


def _production_indexing_components(settings: Settings) -> IndexingComponents:
    return production_retrieval_components(settings)


def _indexing_components(
    settings: Settings,
    *,
    factory: IndexingComponentsFactory | None = None,
) -> IndexingComponents:
    selected_factory = factory or _production_indexing_components
    return selected_factory(settings)


async def _load_chunks(factory, revision_id: UUID) -> list[DocumentChunk]:
    async with factory() as session:
        result = await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_revision_id == revision_id)
            .order_by(DocumentChunk.ordinal, DocumentChunk.chunk_id)
        )
        return list(result)


def _vector_records(
    claim: ClaimedIngestionJob,
    chunks: list[DocumentChunk],
    dense: DenseEmbedder,
    sparse: SparseEncoder,
) -> tuple[VectorRecord, ...]:
    texts = [chunk.text for chunk in chunks]
    dense_vectors = dense.embed_documents(texts)
    sparse_vectors = sparse.encode_documents(texts)
    if len(dense_vectors) != len(chunks) or len(sparse_vectors) != len(chunks):
        raise KnowledgeProviderError(
            "INVALID_PROVIDER_RESULT",
            "The embedding provider returned an invalid result.",
        )
    return tuple(
        VectorRecord(
            point_id=deterministic_point_id(chunk.chunk_id),
            chunk_id=chunk.chunk_id,
            dense=tuple(dense_vectors[index]),
            sparse=sparse_vectors[index],
            payload={
                "workspace_id": str(claim.workspace_id),
                "knowledge_base_id": str(claim.knowledge_base_id),
                "document_id": str(chunk.document_id),
                "document_revision_id": str(chunk.document_revision_id),
                "chunk_id": chunk.chunk_id,
                "locator": chunk.locator,
            },
        )
        for index, chunk in enumerate(chunks)
    )


async def _index_claim(
    factory,
    claim: ClaimedIngestionJob,
    *,
    dense: DenseEmbedder,
    sparse: SparseEncoder,
    index: VectorIndex,
) -> _IndexingOutcome:
    chunks = await _load_chunks(factory, claim.document_revision_id)
    if not chunks:
        return _IndexingOutcome(
            ("EMPTY_CHUNKS", "The document contains no indexed chunks."),
            None,
            False,
            0,
            0,
        )

    # Embeddings are intentionally recomputed for both EMBEDDING and INDEXING.
    # PostgreSQL remains the durable textual source; deterministic Qdrant upsert
    # makes a retry after a worker crash safe and idempotent.
    records = await asyncio.to_thread(_vector_records, claim, chunks, dense, sparse)
    if claim.stage == IngestionStage.EMBEDDING:
        async with factory() as session:
            transitioned = await advance_ingestion_stage(
                session,
                job_id=claim.id,
                lease_token=claim.lease_token,
                expected_stage=IngestionStage.EMBEDDING,
                next_stage=IngestionStage.INDEXING,
            )
        if not transitioned:
            return _IndexingOutcome(None, None, True, len(chunks), len(records))

    await asyncio.to_thread(index.ensure_collection)
    await asyncio.to_thread(index.upsert, records)
    async with factory() as session:
        finalized = await finalize_ingestion_ready(
            session,
            job_id=claim.id,
            lease_token=claim.lease_token,
        )
    return _IndexingOutcome(None, None, not finalized, len(chunks), len(records))


async def _process_knowledge_ingestion(
    job_id: UUID,
    settings: Settings,
    *,
    indexing_factory: IndexingComponentsFactory | None = None,
    trace_sink: TraceSink | None = None,
) -> None:
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

        trace_span = await _safe_trace_start(
            trace_sink or NoopTraceSink(),
            "knowledge.ingest",
            {
                "workspace_id": str(claim.workspace_id),
                "knowledge_base_id": str(claim.knowledge_base_id),
                "document_revision_id": str(claim.document_revision_id),
                "job_id": str(claim.id),
                "stage": claim.stage,
                "attempt_count": claim.attempt_count,
            },
        )
        started = time.perf_counter()
        chunk_count = 0
        point_count = 0
        final_status = "succeeded"
        failure_code: str | None = None
        renewal_task = asyncio.create_task(_renew_lease_loop(factory, claim, settings))
        terminal_error: tuple[str, str] | None = None
        retryable_error: tuple[str, str] | None = None
        stale = False
        try:
            try:
                if claim.stage in (IngestionStage.PARSING, IngestionStage.CHUNKING):
                    content = await _read_blob(LocalBlobStore(settings.blob_root), claim.blob_key)
                    parsed = await ProcessDocumentParser(
                        timeout_seconds=settings.knowledge_parser_timeout_seconds,
                        max_pdf_pages=settings.knowledge_max_pdf_pages,
                        max_parsed_chars=settings.knowledge_max_parsed_chars,
                    ).parse(content, media_type=claim.media_type)
                    if claim.stage == IngestionStage.PARSING:
                        async with factory() as session:
                            transitioned = await advance_ingestion_stage(
                                session,
                                job_id=claim.id,
                                lease_token=claim.lease_token,
                                expected_stage=IngestionStage.PARSING,
                                next_stage=IngestionStage.CHUNKING,
                            )
                        if not transitioned:
                            stale = True
                    if not stale:
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
                                stale = True
                            else:
                                chunk_count = len(chunks)
                                claim = replace(claim, stage=IngestionStage.EMBEDDING)

                if not stale and terminal_error is None:
                    components = _indexing_components(settings, factory=indexing_factory)
                    try:
                        outcome = await _index_claim(
                            factory,
                            claim,
                            dense=components.dense,
                            sparse=components.sparse,
                            index=components.index,
                        )
                        terminal_error = outcome.terminal_error
                        retryable_error = outcome.retryable_error
                        stale = outcome.stale
                        chunk_count = max(chunk_count, outcome.chunk_count)
                        point_count = outcome.point_count
                    except KnowledgeProviderError as exc:
                        if provider_error_is_retryable(exc.code):
                            retryable_error = (exc.code, exc.message)
                        else:
                            terminal_error = (exc.code, exc.message)
            except BlobStoreError:
                retryable_error = (
                    "BLOB_TEMPORARY_FAILURE",
                    "The source blob could not be read temporarily.",
                )
            except DocumentParseError as exc:
                terminal_error = (exc.code, exc.message)
        except SQLAlchemyError:
            logger.warning(
                "knowledge_ingestion_database_failure",
                extra={"job_id": str(job_id)},
                exc_info=True,
            )
            retryable_error = (
                "DATABASE_TEMPORARY_FAILURE",
                "The ingestion worker encountered a temporary database failure.",
            )
        except Exception:
            logger.warning(
                "knowledge_ingestion_worker_error",
                extra={"job_id": str(job_id)},
                exc_info=True,
            )
            terminal_error = (
                "INGESTION_INTERNAL_ERROR",
                "The ingestion worker failed.",
            )
        finally:
            await _cancel_renewal(renewal_task)
            if terminal_error is not None:
                failure_code = terminal_error[0]
            elif retryable_error is not None:
                failure_code = retryable_error[0]
            try:
                if stale:
                    final_status = "stale"
                    failure_code = failure_code or "INGESTION_LEASE_LOST"
                else:
                    final_status = await _finalize_claim(
                        factory,
                        claim,
                        terminal_error=terminal_error,
                        retryable_error=retryable_error,
                        settings=settings,
                    )
            except Exception:
                final_status = "retrying"
                failure_code = failure_code or "DATABASE_TEMPORARY_FAILURE"
                raise
            finally:
                await _safe_trace_end(
                    trace_span,
                    attributes={
                        "workspace_id": str(claim.workspace_id),
                        "knowledge_base_id": str(claim.knowledge_base_id),
                        "document_revision_id": str(claim.document_revision_id),
                        "job_id": str(claim.id),
                        "stage": claim.stage,
                        "attempt_count": claim.attempt_count,
                        "chunk_count": chunk_count,
                        "point_count": point_count,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "status": final_status,
                        "failure_code": failure_code,
                    },
                    status="error" if final_status == "failed" else final_status,
                    failure_code=failure_code,
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
    settings = get_settings()
    asyncio.run(
        _process_knowledge_ingestion(
            parsed_job_id,
            settings,
            indexing_factory=_production_indexing_components,
        )
    )


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
