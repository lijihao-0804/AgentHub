"""Snapshot-scoped hybrid retrieval application service."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.contracts import (
    DenseEmbedder,
    KnowledgeProviderError,
    KnowledgeRetriever,
    RerankCandidate,
    Reranker,
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
    RetrievedEvidence,
    SparseEncoder,
    VectorIndex,
    VectorScope,
    VectorSearchHit,
)
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
)
from packages.observability import NoopTraceSink
from packages.observability.contracts import TraceSink, TraceSpan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FusedCandidate:
    chunk_id: str
    retrieval_score: float
    best_source_rank: int


def _uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise AgentHubError("INVALID_RETRIEVAL_QUERY", f"The {field} is invalid.", 400) from None


async def _safe_span_start(
    sink: TraceSink,
    name: str,
    attributes: Mapping[str, Any],
) -> TraceSpan | None:
    try:
        return await sink.start_span(name, attributes)
    except Exception:
        logger.warning("knowledge_trace_start_failed", extra={"span": name})
        return None


async def _safe_span_end(
    span: TraceSpan | None,
    *,
    attributes: Mapping[str, Any],
    status: str = "ok",
    failure_code: str | None = None,
) -> None:
    if span is None:
        return
    try:
        await span.end(attributes=attributes, status=status, failure_code=failure_code)
    except Exception:
        logger.warning("knowledge_trace_end_failed")


def _payload_chunk_id(hit: VectorSearchHit) -> str | None:
    value = hit.payload.get("chunk_id")
    return value if isinstance(value, str) and value else None


def _ranked_hits(hits: tuple[VectorSearchHit, ...]) -> dict[str, tuple[int, float]]:
    ranked: dict[str, tuple[int, float]] = {}
    for rank, hit in enumerate(hits, start=1):
        chunk_id = _payload_chunk_id(hit)
        if chunk_id is None or not math.isfinite(hit.score):
            continue
        ranked.setdefault(chunk_id, (rank, hit.score))
    return ranked


def _trace_hits(hits: tuple[VectorSearchHit, ...]) -> tuple[RetrievalTraceResult, ...]:
    return tuple(
        RetrievalTraceResult(chunk_id, rank, score)
        for rank, hit in enumerate(hits, start=1)
        for chunk_id in (_payload_chunk_id(hit),)
        if chunk_id is not None and math.isfinite(hit.score)
        for score in (float(hit.score),)
    )


def fuse_reciprocal_rank(
    dense_hits: tuple[VectorSearchHit, ...],
    sparse_hits: tuple[VectorSearchHit, ...],
    *,
    rrf_k: int = 60,
    candidate_top_k: int = 20,
) -> tuple[_FusedCandidate, ...]:
    dense = _ranked_hits(dense_hits)
    sparse = _ranked_hits(sparse_hits)
    fused: dict[str, tuple[float, int]] = {}
    for ranked in (dense, sparse):
        for chunk_id, (rank, _score) in ranked.items():
            score, best_rank = fused.get(chunk_id, (0.0, rank))
            fused[chunk_id] = (score + (1.0 / (rrf_k + rank)), min(best_rank, rank))
    ordered = sorted(
        (
            _FusedCandidate(chunk_id, score, best_rank)
            for chunk_id, (score, best_rank) in fused.items()
        ),
        key=lambda item: (-item.retrieval_score, item.best_source_rank, item.chunk_id),
    )
    return tuple(ordered[:candidate_top_k])


class HybridKnowledgeRetriever(KnowledgeRetriever):
    def __init__(
        self,
        *,
        session: AsyncSession,
        dense_embedder: DenseEmbedder,
        sparse_encoder: SparseEncoder,
        reranker: Reranker,
        vector_index: VectorIndex,
        trace_sink: TraceSink | None = None,
        rrf_k: int = 60,
    ) -> None:
        self.session = session
        self.dense_embedder = dense_embedder
        self.sparse_encoder = sparse_encoder
        self.reranker = reranker
        self.vector_index = vector_index
        self.trace_sink = trace_sink or NoopTraceSink()
        self.rrf_k = rrf_k

    async def _snapshot_scope(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> tuple[UUID, UUID, tuple[str, ...]]:
        workspace_id = _uuid(context.workspace_id, field="workspace_id")
        knowledge_base_id = _uuid(query.knowledge_base_id, field="knowledge_base_id")
        snapshot_id = _uuid(query.knowledge_snapshot_id, field="knowledge_snapshot_id")
        snapshot = await self.session.scalar(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.id == snapshot_id,
                KnowledgeSnapshot.workspace_id == workspace_id,
                KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
            )
        )
        if snapshot is None:
            raise AgentHubError("SNAPSHOT_NOT_FOUND", "The knowledge snapshot was not found.", 404)
        revision_ids = tuple(
            str(item)
            for item in await self.session.scalars(
                select(KnowledgeSnapshotItem.document_revision_id)
                .where(
                    KnowledgeSnapshotItem.workspace_id == workspace_id,
                    KnowledgeSnapshotItem.snapshot_id == snapshot_id,
                    KnowledgeSnapshotItem.knowledge_base_id == knowledge_base_id,
                )
                .order_by(KnowledgeSnapshotItem.document_revision_id)
            )
        )
        return workspace_id, knowledge_base_id, revision_ids

    async def _load_chunks(
        self,
        *,
        workspace_id: UUID,
        knowledge_base_id: UUID,
        revision_ids: tuple[str, ...],
        chunk_ids: set[str],
    ) -> dict[str, tuple[DocumentChunk, Document, DocumentRevision]]:
        if not revision_ids or not chunk_ids:
            return {}
        result = await self.session.execute(
            select(DocumentChunk, Document, DocumentRevision)
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
                DocumentChunk.document_revision_id.in_(tuple(UUID(item) for item in revision_ids)),
                DocumentChunk.chunk_id.in_(chunk_ids),
            )
        )
        return {chunk.chunk_id: (chunk, document, revision) for chunk, document, revision in result}

    async def retrieve_with_trace(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> RetrievalResult:
        if "knowledge_run" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
        if min(
            query.dense_top_k,
            query.sparse_top_k,
            query.candidate_top_k,
            query.final_top_k,
        ) < 1:
            raise AgentHubError(
                "INVALID_RETRIEVAL_QUERY",
                "Retrieval limits must be positive.",
                400,
            )

        workspace_id, knowledge_base_id, revision_ids = await self._snapshot_scope(context, query)
        snapshot_span = await _safe_span_start(
            self.trace_sink,
            "knowledge.retrieve",
            {"snapshot_id": query.knowledge_snapshot_id},
        )
        if not revision_ids:
            await _safe_span_end(
                snapshot_span,
                attributes={
                    "snapshot_id": query.knowledge_snapshot_id,
                    "dense_result_count": 0,
                    "sparse_result_count": 0,
                    "fusion_candidate_count": 0,
                    "rerank_input_count": 0,
                    "rerank_output_count": 0,
                    "total_latency_ms": 0,
                },
            )
            empty_stage = RetrievalTraceStage(latency_ms=0, results=())
            return RetrievalResult(
                evidence=(),
                trace=RetrievalTrace(
                    snapshot_id=query.knowledge_snapshot_id,
                    dense=empty_stage,
                    sparse=empty_stage,
                    fusion=empty_stage,
                    rerank=empty_stage,
                    total_latency_ms=0,
                ),
            )

        scope = VectorScope(
            workspace_id=str(workspace_id),
            knowledge_base_id=str(knowledge_base_id),
            document_revision_ids=revision_ids,
        )
        started = time.perf_counter()
        rerank_span: TraceSpan | None = None
        stage = "dense"
        try:
            dense_started = time.perf_counter()
            dense_query = await asyncio.to_thread(self.dense_embedder.embed_query, query.text)
            dense_hits = await asyncio.to_thread(
                self.vector_index.dense_search,
                dense_query,
                scope=scope,
                limit=query.dense_top_k,
            )
            dense_latency_ms = (time.perf_counter() - dense_started) * 1000

            stage = "sparse"
            sparse_started = time.perf_counter()
            sparse_query = await asyncio.to_thread(self.sparse_encoder.encode_query, query.text)
            sparse_hits = await asyncio.to_thread(
                self.vector_index.sparse_search,
                sparse_query,
                scope=scope,
                limit=query.sparse_top_k,
            )
            sparse_latency_ms = (time.perf_counter() - sparse_started) * 1000

            stage = "fusion"
            fusion_started = time.perf_counter()
            fused = fuse_reciprocal_rank(
                dense_hits,
                sparse_hits,
                rrf_k=self.rrf_k,
                candidate_top_k=query.candidate_top_k,
            )
            fusion_latency_ms = (time.perf_counter() - fusion_started) * 1000
            fusion_trace_results = tuple(
                RetrievalTraceResult(item.chunk_id, rank, item.retrieval_score)
                for rank, item in enumerate(fused, start=1)
            )
            chunk_map = await self._load_chunks(
                workspace_id=workspace_id,
                knowledge_base_id=knowledge_base_id,
                revision_ids=revision_ids,
                chunk_ids={item.chunk_id for item in fused},
            )
            candidates = tuple(
                RerankCandidate(item.chunk_id, chunk_map[item.chunk_id][0].text)
                for item in fused
                if item.chunk_id in chunk_map
            )
            stage = "rerank"
            rerank_span = await _safe_span_start(
                self.trace_sink,
                "knowledge.rerank",
                {
                    "snapshot_id": query.knowledge_snapshot_id,
                    "candidate_count": len(candidates),
                },
            )
            rerank_started = time.perf_counter()
            rerank_scores = await asyncio.to_thread(
                self.reranker.rerank,
                query.text,
                candidates,
            )
            if len(rerank_scores) != len(candidates) or not all(
                math.isfinite(score) for score in rerank_scores
            ):
                raise KnowledgeProviderError(
                    "INVALID_RERANK_RESULT",
                    "The reranker returned an invalid result.",
                )
            rerank_latency_ms = (time.perf_counter() - rerank_started) * 1000
            fused_by_chunk = {item.chunk_id: item for item in fused}
            ranked = sorted(
                zip(candidates, rerank_scores, strict=True),
                key=lambda item: (
                    -item[1],
                    -fused_by_chunk[item[0].chunk_id].retrieval_score,
                    item[0].chunk_id,
                ),
            )[: query.final_top_k]
            rerank_trace_results = tuple(
                RetrievalTraceResult(candidate.chunk_id, rank, float(rerank_score))
                for rank, (candidate, rerank_score) in enumerate(ranked, start=1)
            )
            evidence: list[RetrievedEvidence] = []
            for candidate, rerank_score in ranked:
                chunk, document, revision = chunk_map[candidate.chunk_id]
                evidence.append(
                    RetrievedEvidence(
                        document_id=str(document.id),
                        document_revision_id=str(revision.id),
                        chunk_id=chunk.chunk_id,
                        source=f"document:{document.id}/revision:{revision.id}",
                        locator=dict(chunk.locator),
                        text=chunk.text,
                        retrieval_score=fused_by_chunk[chunk.chunk_id].retrieval_score,
                        rerank_score=rerank_score,
                        metadata={
                            "ordinal": chunk.ordinal,
                            "normalized_content_hash": chunk.normalized_content_hash,
                        },
                    )
                )
            await _safe_span_end(
                rerank_span,
                attributes={
                    "snapshot_id": query.knowledge_snapshot_id,
                    "candidate_count": len(candidates),
                    "final_count": len(evidence),
                    "latency_ms": round(rerank_latency_ms, 3),
                },
            )
            rerank_span = None
            total_latency_ms = (time.perf_counter() - started) * 1000
            await _safe_span_end(
                snapshot_span,
                attributes={
                    "snapshot_id": query.knowledge_snapshot_id,
                    "dense_latency_ms": round(dense_latency_ms, 3),
                    "dense_result_count": len(dense_hits),
                    "sparse_latency_ms": round(sparse_latency_ms, 3),
                    "sparse_result_count": len(sparse_hits),
                    "fusion_latency_ms": round(fusion_latency_ms, 3),
                    "fusion_candidate_count": len(fused),
                    "rerank_latency_ms": round(rerank_latency_ms, 3),
                    "rerank_input_count": len(candidates),
                    "rerank_output_count": len(evidence),
                    "total_latency_ms": round(total_latency_ms, 3),
                },
            )
            return RetrievalResult(
                evidence=tuple(evidence),
                trace=RetrievalTrace(
                    snapshot_id=query.knowledge_snapshot_id,
                    dense=RetrievalTraceStage(
                        latency_ms=round(dense_latency_ms, 3),
                        results=_trace_hits(dense_hits),
                    ),
                    sparse=RetrievalTraceStage(
                        latency_ms=round(sparse_latency_ms, 3),
                        results=_trace_hits(sparse_hits),
                    ),
                    fusion=RetrievalTraceStage(
                        latency_ms=round(fusion_latency_ms, 3),
                        results=fusion_trace_results,
                    ),
                    rerank=RetrievalTraceStage(
                        latency_ms=round(rerank_latency_ms, 3),
                        results=rerank_trace_results,
                    ),
                    total_latency_ms=round(total_latency_ms, 3),
                ),
            )
        except KnowledgeProviderError as exc:
            await _safe_span_end(
                rerank_span,
                attributes={"snapshot_id": query.knowledge_snapshot_id},
                status="error",
                failure_code=exc.code,
            )
            rerank_span = None
            await _safe_span_end(
                snapshot_span,
                attributes={"snapshot_id": query.knowledge_snapshot_id},
                status="error",
                failure_code=exc.code,
            )
            raise AgentHubError(
                "KNOWLEDGE_INDEX_UNAVAILABLE",
                "The knowledge index is unavailable.",
                503,
            ) from None
        except Exception:
            logger.exception(
                "knowledge_retrieval_failed",
                extra={
                    "snapshot_id": query.knowledge_snapshot_id,
                    "stage": stage,
                },
            )
            await _safe_span_end(
                rerank_span,
                attributes={"snapshot_id": query.knowledge_snapshot_id},
                status="error",
                failure_code="RERANKER_INFERENCE_FAILED",
            )
            rerank_span = None
            await _safe_span_end(
                snapshot_span,
                attributes={"snapshot_id": query.knowledge_snapshot_id},
                status="error",
                failure_code="KNOWLEDGE_INDEX_UNAVAILABLE",
            )
            raise AgentHubError(
                "KNOWLEDGE_INDEX_UNAVAILABLE",
                "The knowledge index is unavailable.",
                503,
            ) from None

    async def retrieve(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> list[RetrievedEvidence]:
        result = await self.retrieve_with_trace(context, query)
        return list(result.evidence)


class SessionScopedKnowledgeRetriever(KnowledgeRetriever):
    """Stateless facade that creates a short-lived DB session per retrieval."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        components,
        trace_sink: TraceSink | None = None,
        rrf_k: int = 60,
    ) -> None:
        self.session_factory = session_factory
        self.components = components
        self.trace_sink = trace_sink
        self.rrf_k = rrf_k

    async def retrieve_with_trace(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> RetrievalResult:
        async with self.session_factory() as session:
            return await HybridKnowledgeRetriever(
                session=session,
                dense_embedder=self.components.dense,
                sparse_encoder=self.components.sparse,
                reranker=self.components.reranker,
                vector_index=self.components.index,
                trace_sink=self.trace_sink,
                rrf_k=self.rrf_k,
            ).retrieve_with_trace(context, query)

    async def retrieve(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> list[RetrievedEvidence]:
        result = await self.retrieve_with_trace(context, query)
        return list(result.evidence)


__all__ = ["HybridKnowledgeRetriever", "SessionScopedKnowledgeRetriever", "fuse_reciprocal_rank"]
