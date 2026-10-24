"""Explicit composition adapter for the experimental original-evidence selector.

No public profile or default composition installs this adapter. The wrapped
retriever still owns snapshot scope, hydration, lifecycle penalties and floor.
"""

from __future__ import annotations

import time
from dataclasses import asdict, replace

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import (
    KnowledgeRetriever,
    RetrievalQuery,
    RetrievalResult,
    RetrievalTraceResult,
    RetrievalTraceStage,
)
from packages.knowledge.evidence_set import EvidenceSetSelector


class EvidenceSetKnowledgeRetriever:
    """Select a joint subset, preserving BGE order and every original source DTO.

    This is the frozen rank-order method, not the reading-order diagnostic.
    Failures propagate; there is no hidden baseline or provider fallback here.
    """

    def __init__(self, retriever: KnowledgeRetriever, selector: EvidenceSetSelector) -> None:
        self.retriever, self.selector = retriever, selector

    async def retrieve_with_trace(
        self, context: WorkspaceExecutionContext, query: RetrievalQuery
    ) -> RetrievalResult:
        if (
            type(query.final_top_k) is not int
            or not 1 <= query.final_top_k <= 10
            or type(query.candidate_top_k) is not int
            or not 1 <= query.candidate_top_k <= 20
            or type(query.context_budget_bytes) is not int
            or not 2 <= query.context_budget_bytes <= 64000
            or query.evidence_projection is not None
        ):
            raise ValueError(
                "joint evidence selection requires its bounded original-record profile"
            )
        # Fetch only the original candidate budget, under the SAME query/snapshot/floor.
        # No final consumer budget is applied until joint subset selection.
        pool = await self.retriever.retrieve_with_trace(
            context, replace(query, final_top_k=query.candidate_top_k, context_budget_bytes=None)
        )
        started = time.perf_counter()
        plan = await self.selector.select(
            context, query.text, pool.evidence, max_items=query.final_top_k
        )
        # Fail closed if a nonstandard selector violates the source-only contract.
        originals = {item.chunk_id: item for item in pool.evidence}
        if (
            len(plan.rank_order) > query.final_top_k
            or len({item.chunk_id for item in plan.rank_order}) != len(plan.rank_order)
            or any(item != originals.get(item.chunk_id) for item in plan.rank_order)
        ):
            raise ValueError("selected evidence differs from authorized original candidates")
        chosen_ids = {item.chunk_id for item in plan.rank_order}
        ranked = tuple(item for item in pool.evidence if item.chunk_id in chosen_ids)
        records = select_budgeted_records(
            [asdict(item) for item in ranked],
            max_bytes=query.context_budget_bytes,
            max_items=query.final_top_k,
        )
        kept_ids = {r["chunk_id"] for r in records}
        evidence = tuple(item for item in ranked if item.chunk_id in kept_ids)
        elapsed = (time.perf_counter() - started) * 1000
        return RetrievalResult(
            evidence,
            replace(
                pool.trace,
                total_latency_ms=pool.trace.total_latency_ms + elapsed,
                evidence_set=RetrievalTraceStage(
                    elapsed,
                    tuple(
                        RetrievalTraceResult(
                            item.chunk_id,
                            i,
                            item.rerank_score
                            if item.rerank_score is not None
                            else item.retrieval_score,
                        )
                        for i, item in enumerate(evidence, 1)
                    ),
                    {
                        "candidate_count": len(pool.evidence),
                        "selected_count": len(plan.rank_order),
                        "admitted_count": len(evidence),
                        "order": "original_relevance",
                    },
                ),
            ),
        )
