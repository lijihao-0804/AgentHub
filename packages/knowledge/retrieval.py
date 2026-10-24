"""Snapshot-scoped hybrid retrieval application service."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import (
    DenseEmbedder,
    KnowledgeProviderError,
    KnowledgeRetriever,
    RerankCandidate,
    Reranker,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStrategy,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
    RetrievedEvidence,
    SparseEncoder,
    VectorIndex,
    VectorScope,
    VectorSearchHit,
)
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.knowledge.evidence_records import evidence_record, evidence_reference
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


def _validate_positive_optional(value: int | None, *, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise AgentHubError(
            "INVALID_RETRIEVAL_QUERY",
            f"Retrieval {field} must be a positive integer.",
            400,
        )


def _candidate_budgets(query: RetrievalQuery) -> tuple[int, int | None]:
    _validate_positive_optional(query.rerank_top_k, field="rerank_top_k")
    _validate_positive_optional(
        query.max_chunks_per_document,
        field="max_chunks_per_document",
    )
    rerank_top_k = query.candidate_top_k if query.rerank_top_k is None else query.rerank_top_k
    if rerank_top_k > query.candidate_top_k:
        raise AgentHubError(
            "INVALID_RETRIEVAL_QUERY",
            "Retrieval rerank_top_k must not exceed candidate_top_k.",
            400,
        )
    return rerank_top_k, query.max_chunks_per_document


def _validate_context_budget_bytes(value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 64_000
    ):
        raise AgentHubError(
            "INVALID_RETRIEVAL_QUERY",
            "Retrieval context_budget_bytes must be an integer from 2 to 64000.",
            400,
        )


def select_rerank_candidates(
    candidates: Sequence[_FusedCandidate],
    *,
    document_ids: Mapping[str, str],
    limit: int,
    max_chunks_per_document: int | None = None,
) -> tuple[_FusedCandidate, ...]:
    """Select hydrated candidates without changing their fusion order.

    Candidates missing from ``document_ids`` are not hydrated and therefore
    neither selected nor counted against any document's quota. The first pass
    enforces the optional soft per-document cap. The second pass admits
    deferred chunks in their original fusion order until ``limit`` is full.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if max_chunks_per_document is not None and (
        isinstance(max_chunks_per_document, bool)
        or not isinstance(max_chunks_per_document, int)
        or max_chunks_per_document < 1
    ):
        raise ValueError("max_chunks_per_document must be a positive integer")

    selected: set[str] = set()
    counts: dict[str, int] = {}
    if max_chunks_per_document is not None:
        for candidate in candidates:
            document_id = document_ids.get(candidate.chunk_id)
            if document_id is None or candidate.chunk_id in selected:
                continue
            count = counts.get(document_id, 0)
            if count >= max_chunks_per_document:
                continue
            if len(selected) >= limit:
                continue
            selected.add(candidate.chunk_id)
            counts[document_id] = count + 1

    if len(selected) < limit:
        for candidate in candidates:
            if len(selected) >= limit:
                break
            if candidate.chunk_id not in selected and candidate.chunk_id in document_ids:
                selected.add(candidate.chunk_id)

    result: list[_FusedCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.chunk_id in selected and candidate.chunk_id not in seen:
            result.append(candidate)
            seen.add(candidate.chunk_id)
    return tuple(result)


class HybridKnowledgeRetriever(KnowledgeRetriever):
    def __init__(
        self,
        *,
        session: AsyncSession | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        dense_embedder: DenseEmbedder,
        sparse_encoder: SparseEncoder,
        reranker: Reranker,
        vector_index: VectorIndex,
        trace_sink: TraceSink | None = None,
        rrf_k: int = 60,
        min_rerank_score: float | None = None,
        superseded_rank_penalty: float = 0.0,
    ) -> None:
        if (session is None) == (session_factory is None):
            raise ValueError("provide exactly one of session or session_factory")
        if superseded_rank_penalty < 0:
            raise ValueError("superseded_rank_penalty must not be negative")
        self.session = session
        self.session_factory = session_factory
        self.dense_embedder = dense_embedder
        self.sparse_encoder = sparse_encoder
        self.reranker = reranker
        self.vector_index = vector_index
        self.trace_sink = trace_sink or NoopTraceSink()
        self.rrf_k = rrf_k
        self.min_rerank_score = min_rerank_score
        self.superseded_rank_penalty = superseded_rank_penalty

    @asynccontextmanager
    async def _db_session(self):
        if self.session is not None:
            yield self.session
            return
        assert self.session_factory is not None
        async with self.session_factory() as session:
            yield session

    async def _snapshot_scope(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> tuple[UUID, UUID, tuple[str, ...]]:
        workspace_id = _uuid(context.workspace_id, field="workspace_id")
        knowledge_base_id = _uuid(query.knowledge_base_id, field="knowledge_base_id")
        snapshot_id = _uuid(query.knowledge_snapshot_id, field="knowledge_snapshot_id")
        snapshot = await session.scalar(
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
            for item in await session.scalars(
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
        session: AsyncSession,
        *,
        snapshot_id: UUID,
        workspace_id: UUID,
        knowledge_base_id: UUID,
        revision_ids: tuple[str, ...],
        chunk_ids: set[str],
    ) -> dict[str, tuple[DocumentChunk, Document, DocumentRevision]]:
        if not revision_ids or not chunk_ids:
            return {}
        result = await session.execute(
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
            .join(
                KnowledgeSnapshotItem,
                (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
                & (KnowledgeSnapshotItem.knowledge_base_id == DocumentChunk.knowledge_base_id)
                & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
                & (
                    KnowledgeSnapshotItem.document_revision_id == DocumentChunk.document_revision_id
                ),
            )
            .where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.knowledge_base_id == knowledge_base_id,
                DocumentChunk.chunk_id.in_(chunk_ids),
                KnowledgeSnapshotItem.workspace_id == workspace_id,
                KnowledgeSnapshotItem.knowledge_base_id == knowledge_base_id,
                KnowledgeSnapshotItem.snapshot_id == snapshot_id,
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
        if (
            min(
                query.dense_top_k,
                query.sparse_top_k,
                query.candidate_top_k,
                query.final_top_k,
            )
            < 1
        ):
            raise AgentHubError(
                "INVALID_RETRIEVAL_QUERY",
                "Retrieval limits must be positive.",
                400,
            )
        try:
            strategy = RetrievalStrategy(query.strategy)
        except ValueError:
            raise AgentHubError(
                "INVALID_RETRIEVAL_QUERY", "The retrieval strategy is invalid.", 400
            ) from None
        rerank_top_k, max_chunks_per_document = _candidate_budgets(query)
        _validate_context_budget_bytes(query.context_budget_bytes)
        if query.evidence_projection is not None and (
            query.evidence_projection != "compact_v1"
            or query.context_budget_bytes is None
            or query.final_top_k > 10
            or query.candidate_top_k > 100
        ):
            raise AgentHubError(
                "INVALID_RETRIEVAL_QUERY", "The evidence projection is invalid.", 400
            )

        async with self._db_session() as session:
            workspace_id, knowledge_base_id, revision_ids = await self._snapshot_scope(
                session, context, query
            )
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
                    "selection_latency_ms": 0,
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

            sparse_hits: tuple[VectorSearchHit, ...] = ()
            sparse_latency_ms = 0.0
            if strategy is not RetrievalStrategy.DENSE:
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

            fusion_latency_ms = 0.0
            fusion_trace_results: tuple[RetrievalTraceResult, ...] = ()
            if strategy is RetrievalStrategy.DENSE:
                fused = tuple(
                    _FusedCandidate(chunk_id, float(hit.score), rank)
                    for rank, hit in enumerate(dense_hits, start=1)
                    for chunk_id in (_payload_chunk_id(hit),)
                    if chunk_id is not None and math.isfinite(hit.score)
                )[: query.candidate_top_k]
            else:
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
            async with self._db_session() as session:
                chunk_map = await self._load_chunks(
                    session,
                    snapshot_id=UUID(query.knowledge_snapshot_id),
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
            hydrated_candidate_count = len(candidates)
            selected_fused = fused
            selection_trace: RetrievalTraceStage | None = None
            selection_latency_ms = 0.0
            rerank_latency_ms = 0.0
            rerank_trace_results: tuple[RetrievalTraceResult, ...] = ()
            fused_by_chunk = {item.chunk_id: item for item in fused}
            if strategy is RetrievalStrategy.HYBRID_RERANK:
                selection_started = time.perf_counter()
                selected_fused = select_rerank_candidates(
                    fused,
                    document_ids={
                        item.chunk_id: str(chunk_map[item.chunk_id][1].id)
                        for item in fused
                        if item.chunk_id in chunk_map
                    },
                    limit=rerank_top_k,
                    max_chunks_per_document=max_chunks_per_document,
                )
                selected_ids = {item.chunk_id for item in selected_fused}
                candidates = tuple(
                    candidate for candidate in candidates if candidate.chunk_id in selected_ids
                )
                selection_latency_ms = (time.perf_counter() - selection_started) * 1000
                selection_trace = RetrievalTraceStage(
                    latency_ms=round(selection_latency_ms, 3),
                    results=tuple(
                        RetrievalTraceResult(item.chunk_id, rank, item.retrieval_score)
                        for rank, item in enumerate(selected_fused, start=1)
                    ),
                )
                stage = "rerank"
                rerank_span = await _safe_span_start(
                    self.trace_sink,
                    "knowledge.rerank",
                    {
                        "snapshot_id": query.knowledge_snapshot_id,
                        "candidate_count": hydrated_candidate_count,
                        "rerank_input_count": len(candidates),
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
                # The supersession penalty is applied before the final count
                # ceiling: the point is to change which chunks win the slots,
                # not to reorder the ones that already won. A superseded
                # document can still be returned -- "what did the old policy
                # say" is a legitimate question -- it just has to out-score the
                # live one by more than the penalty.
                if self.superseded_rank_penalty:
                    rerank_scores = tuple(
                        score - self.superseded_rank_penalty
                        if chunk_map[candidate.chunk_id][1].superseded_by_document_id is not None
                        else score
                        for candidate, score in zip(candidates, rerank_scores, strict=True)
                    )
                ranked = sorted(
                    zip(candidates, rerank_scores, strict=True),
                    key=lambda item: (
                        -item[1],
                        -fused_by_chunk[item[0].chunk_id].retrieval_score,
                        item[0].chunk_id,
                    ),
                )
                # An opted-in byte budget needs the full sorted pool so an
                # oversized high-ranked record can be skipped and a later
                # record can fill the count-limited result. Without the budget,
                # retain the historical cut before floor and trace projection.
                if query.context_budget_bytes is None:
                    ranked = ranked[: query.final_top_k]
                rerank_trace_results = tuple(
                    RetrievalTraceResult(candidate.chunk_id, rank, float(rerank_score))
                    for rank, (candidate, rerank_score) in enumerate(ranked, start=1)
                )
            else:
                ranked = [(candidate, None) for candidate in candidates]
                if query.context_budget_bytes is None:
                    ranked = ranked[: query.final_top_k]
            # The floor is applied *after* ranking, and only where a rerank
            # score exists: the fusion score is an RRF rank statistic on an
            # unrelated scale, so the same number would mean nothing there.
            # ``rerank_trace_results`` above is deliberately left pre-floor.
            # With a byte budget it is also pre-budget and pre-count-ceiling so
            # the trace shows the full ranked pool considered for backfill;
            # without one it retains the historical final_top_k-sized trace.
            floor = (
                query.min_rerank_score
                if query.min_rerank_score is not None
                else self.min_rerank_score
            )
            dropped_below_floor = 0
            if floor is not None:
                kept = [item for item in ranked if item[1] is None or item[1] >= floor]
                dropped_below_floor = len(ranked) - len(kept)
                ranked = kept
            ranked_evidence: list[RetrievedEvidence] = []
            for candidate, rerank_score in ranked:
                chunk, document, revision = chunk_map[candidate.chunk_id]
                superseded_by = document.superseded_by_document_id
                ranked_evidence.append(
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
                            # Lifecycle travels with the evidence because the
                            # model is the last reader that can act on it: a
                            # chunk carries no hint that its document was
                            # replaced, and ranking alone cannot decide that a
                            # superseded document is the wrong answer.
                            "document_name": document.name,
                            "effective_date": (
                                document.effective_date.isoformat()
                                if document.effective_date is not None
                                else None
                            ),
                            "superseded": superseded_by is not None,
                            "superseded_by_document_id": (
                                str(superseded_by) if superseded_by is not None else None
                            ),
                        },
                    )
                )
            evidence = ranked_evidence
            context_selection_metadata: dict[str, Any] | None = None
            if query.context_budget_bytes is not None:
                if query.evidence_projection == "compact_v1":
                    plan = assemble_evidence_plan(
                        [
                            evidence_record(item, snapshot_id=query.knowledge_snapshot_id)
                            for item in ranked_evidence
                        ],
                        [
                            evidence_reference(
                                item,
                                snapshot_id=query.knowledge_snapshot_id,
                                knowledge_base_id=query.knowledge_base_id,
                            )
                            for item in ranked_evidence
                        ],
                        budget=query.context_budget_bytes,
                        estimator=lambda text: len(text.encode("utf-8")),
                        citation_prefix="R00001E",
                        max_items=query.final_top_k,
                    )
                    if plan is None:
                        selected_records = ()
                        selected_bytes = 0
                    else:
                        selected_records = tuple(
                            value["record"] for value in plan.registry.values()
                        )
                        selected_bytes = len(
                            json.dumps(
                                plan.payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode("utf-8")
                        )
                else:
                    selected_records = select_budgeted_records(
                        [asdict(item) for item in ranked_evidence],
                        max_bytes=query.context_budget_bytes,
                        max_items=query.final_top_k,
                    )
                    selected_bytes = len(
                        json.dumps(
                            list(selected_records),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                    )
                selected_ids = [record["chunk_id"] for record in selected_records]
                selected_id_set = set(selected_ids)
                evidence = [item for item in ranked_evidence if item.chunk_id in selected_id_set]
                context_selection_metadata = {
                    "limit": query.context_budget_bytes,
                    "before": len(ranked_evidence),
                    "after": len(evidence),
                    "bytes": selected_bytes,
                }
                if query.evidence_projection == "compact_v1":
                    context_selection_metadata["projection"] = "rag_view_v1"
            if rerank_span is not None:
                await _safe_span_end(
                    rerank_span,
                    attributes={
                        "snapshot_id": query.knowledge_snapshot_id,
                        "candidate_count": hydrated_candidate_count,
                        "rerank_input_count": len(candidates),
                        "final_count": len(evidence),
                        "latency_ms": round(rerank_latency_ms, 3),
                        "selection_latency_ms": round(selection_latency_ms, 3),
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
                    "selection_latency_ms": round(selection_latency_ms, 3),
                    "rerank_latency_ms": round(rerank_latency_ms, 3),
                    "rerank_input_count": len(candidates),
                    "rerank_output_count": len(evidence),
                    "dropped_below_floor": dropped_below_floor,
                    "context_budget_bytes": query.context_budget_bytes,
                    "context_selection_before": (
                        context_selection_metadata["before"]
                        if context_selection_metadata is not None
                        else None
                    ),
                    "context_selection_after": (
                        context_selection_metadata["after"]
                        if context_selection_metadata is not None
                        else None
                    ),
                    "context_selection_bytes": (
                        context_selection_metadata["bytes"]
                        if context_selection_metadata is not None
                        else None
                    ),
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
                        metadata=context_selection_metadata,
                    ),
                    total_latency_ms=round(total_latency_ms, 3),
                    dropped_below_floor=dropped_below_floor,
                    selection=selection_trace,
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
        min_rerank_score: float | None = None,
        superseded_rank_penalty: float = 0.0,
    ) -> None:
        self.session_factory = session_factory
        self.components = components
        self.trace_sink = trace_sink
        self.rrf_k = rrf_k
        self.min_rerank_score = min_rerank_score
        self.superseded_rank_penalty = superseded_rank_penalty

    async def retrieve_with_trace(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> RetrievalResult:
        return await HybridKnowledgeRetriever(
            session_factory=self.session_factory,
            dense_embedder=self.components.dense,
            sparse_encoder=self.components.sparse,
            reranker=self.components.reranker,
            vector_index=self.components.index,
            trace_sink=self.trace_sink,
            rrf_k=self.rrf_k,
            min_rerank_score=self.min_rerank_score,
            superseded_rank_penalty=self.superseded_rank_penalty,
        ).retrieve_with_trace(context, query)

    async def retrieve(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> list[RetrievedEvidence]:
        result = await self.retrieve_with_trace(context, query)
        return list(result.evidence)


__all__ = [
    "HybridKnowledgeRetriever",
    "SessionScopedKnowledgeRetriever",
    "fuse_reciprocal_rank",
    "select_rerank_candidates",
]
