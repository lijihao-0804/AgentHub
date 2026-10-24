"""Explicit composition adapter for the opt-in Onyx source-section selector.

Opt-in only: no default composition installs this wrapper. The wrapped
retriever still owns snapshot scope, hydration, lifecycle penalties, floor
and dedup. Any selector failure falls back to the exact original ranking and
budget selection, marked in the trace -- the fallback never fabricates a
successful selection.
"""

from __future__ import annotations

import time
from dataclasses import asdict, replace

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.adapters.onyx_selection import (
    SectionSelectorError,
    promote_evidence,
    sections_from_evidence,
)
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import (
    KnowledgeRetriever,
    RetrievalQuery,
    RetrievalResult,
    RetrievalTraceResult,
    RetrievalTraceStage,
    SectionSelector,
)


class OnyxSelectionKnowledgeRetriever:
    """Promote source groups a section-selection model chose, or fall back.

    The inner call fetches the candidate-sized pool under the SAME
    query/snapshot/floor/dedup semantics; only the final ordering and the
    count/byte cut happen here, exactly like the joint-evidence combinator.
    """

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        selector: SectionSelector | None,
    ) -> None:
        self.retriever = retriever
        self.selector = selector

    async def retrieve_with_trace(
        self, context: WorkspaceExecutionContext, query: RetrievalQuery
    ) -> RetrievalResult:
        if (
            type(query.final_top_k) is not int
            or not 1 <= query.final_top_k <= 10
            or type(query.candidate_top_k) is not int
            or not 1 <= query.candidate_top_k <= 20
            or type(query.context_budget_bytes) is not int
            or not 2 <= query.context_budget_bytes <= 64_000
            or query.evidence_projection is not None
        ):
            raise ValueError("onyx selection requires its bounded original-record profile")
        pool = await self.retriever.retrieve_with_trace(
            context, replace(query, final_top_k=query.candidate_top_k, context_budget_bytes=None)
        )
        if not pool.evidence:
            return pool
        started = time.perf_counter()
        selected: tuple[int, ...] | None = None
        fallback_reason: str | None = None
        if self.selector is None:
            fallback_reason = "unconfigured"
        else:
            try:
                sections = sections_from_evidence(pool.evidence)
                selected = await self.selector.select_sections(
                    context, query.text, sections
                )
                ranked = promote_evidence(pool.evidence, sections, selected)
            except SectionSelectorError as exc:
                selected, fallback_reason = None, f"selector_failed:{type(exc).__name__}"
            except ValueError as exc:
                # A malformed selection output surfaces as ValueError from
                # strict parsing; a malformed pool must not leak either.
                selected, fallback_reason = None, f"invalid_selection:{exc.__class__.__name__}"
        if selected is None:
            ranked = pool.evidence
        elapsed = (time.perf_counter() - started) * 1000
        records = select_budgeted_records(
            [asdict(item) for item in ranked],
            max_bytes=query.context_budget_bytes,
            max_items=query.final_top_k,
        )
        kept_ids = {record["chunk_id"] for record in records}
        evidence = tuple(item for item in ranked if item.chunk_id in kept_ids)
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
                            rank,
                            item.rerank_score
                            if item.rerank_score is not None
                            else item.retrieval_score,
                        )
                        for rank, item in enumerate(evidence, 1)
                    ),
                    {
                        "selector": "onyx_v1",
                        "status": "selected" if selected is not None else "fallback",
                        **({"reason": fallback_reason} if fallback_reason else {}),
                        "candidate_count": len(pool.evidence),
                        **(
                            {"selected_sections": len(selected)}
                            if selected is not None
                            else {}
                        ),
                        "admitted_count": len(evidence),
                        "order": "original_relevance",
                    },
                ),
            ),
        )


__all__ = ["OnyxSelectionKnowledgeRetriever"]
