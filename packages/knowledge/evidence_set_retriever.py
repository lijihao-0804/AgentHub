"""Explicit composition adapter for the experimental original-evidence selector.

No public profile or default composition installs this adapter. The wrapped
retriever still owns snapshot scope, hydration, lifecycle penalties and floor.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, replace

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.call_scope import (
    auxiliary_model_attempt,
    remaining_call_seconds,
    unpriced_auxiliary_calls_allowed,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import (
    MAX_EVIDENCE_ITEMS,
    MAX_SELECTION_CANDIDATES,
    KnowledgeRetriever,
    RetrievalQuery,
    RetrievalResult,
    RetrievalTraceResult,
    RetrievalTraceStage,
)
from packages.knowledge.evidence_set import EvidenceSetSelector
from packages.model_gateway.contracts import (
    ModelRequest,
    ModelResponse,
    ResolvedModelExecutionPlan,
    ResolvedModelGenerator,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode


class _AccountedSelectorGateway:
    def __init__(self, gateway: ResolvedModelGenerator):
        self.gateway = gateway

    async def generate_resolved(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> ModelResponse:
        # ContextBudgetPolicy has admitted the entire selector message set here.
        with auxiliary_model_attempt(
            source="complementary_selector",
            provider=plan.primary.provider,
            model=plan.primary.model,
        ) as attempt:
            try:
                response = await self.gateway.generate_resolved(context, plan, request)
            except (asyncio.CancelledError, TimeoutError):
                attempt["attempt_status"] = "UNKNOWN_TIMEOUT"
                raise
            except ModelGatewayError as exc:
                attempt["attempt_status"] = (
                    "UNKNOWN_TIMEOUT"
                    if exc.code == ModelGatewayErrorCode.MODEL_TIMEOUT
                    else "FAILED"
                )
                raise
            attempt["usage"] = asdict(response.usage) if response.usage is not None else None
            attempt["cost"] = (
                asdict(response.cost_estimate) if response.cost_estimate is not None else None
            )
            attempt["attempt_status"] = "SUCCEEDED"
            return response


class EvidenceSetKnowledgeRetriever:
    """Select a joint subset, preserving BGE order and every original source DTO.

    This is the frozen rank-order method, not the reading-order diagnostic.
    Direct compositions propagate failures. Published tool opt-ins explicitly
    recover the original scoped ranking without repeating the retrieval or model call.
    """

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        selector: EvidenceSetSelector | None,
        *,
        product_fallback: bool = False,
    ) -> None:
        self.retriever, self.selector = retriever, selector
        self.product_fallback = product_fallback

    @classmethod
    def for_product(
        cls,
        retriever: KnowledgeRetriever,
        binding: tuple[ResolvedModelGenerator, ResolvedModelExecutionPlan] | None,
        context: WorkspaceExecutionContext,
    ) -> EvidenceSetKnowledgeRetriever:
        selector = None
        if binding is not None:
            gateway, plan = binding
            if str(plan.primary.workspace_id) != context.workspace_id:
                raise ValueError("selector workspace differs from authorized tool workspace")
            selector = EvidenceSetSelector(
                _AccountedSelectorGateway(gateway),
                ResolvedModelExecutionPlan(
                    replace(plan.primary, max_tokens=min(256, plan.primary.max_tokens))
                ),
            )
        return cls(retriever, selector, product_fallback=True)

    async def retrieve_with_trace(
        self, context: WorkspaceExecutionContext, query: RetrievalQuery
    ) -> RetrievalResult:
        if (
            type(query.final_top_k) is not int
            or not 1 <= query.final_top_k <= MAX_EVIDENCE_ITEMS
            or type(query.candidate_top_k) is not int
            or not 1 <= query.candidate_top_k <= MAX_SELECTION_CANDIDATES
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
        reason = None
        plan = None
        if self.product_fallback and self.selector is None:
            reason = "unconfigured"
        elif self.product_fallback and not unpriced_auxiliary_calls_allowed():
            reason = "cost_budget_unpriced_selector"
        else:
            if self.selector is None:
                raise ValueError("selector is missing")
            try:
                remaining = remaining_call_seconds() if self.product_fallback else None
                if remaining is not None and remaining <= 0.1:
                    raise TimeoutError
                async with asyncio.timeout(None if remaining is None else remaining - 0.1):
                    plan = await self.selector.select(
                        context, query.text, pool.evidence, max_items=query.final_top_k
                    )
            except (TimeoutError, ValueError, ModelGatewayError, AgentHubError) as exc:
                if not self.product_fallback:
                    raise
                if isinstance(exc, AgentHubError) and exc.code != "AGENT_CONTEXT_BUDGET_EXCEEDED":
                    raise
                if isinstance(exc, ModelGatewayError) and exc.code in {
                    ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID,
                    ModelGatewayErrorCode.MODEL_TARGET_FORBIDDEN,
                    ModelGatewayErrorCode.MODEL_AUTH_FAILED,
                }:
                    raise
                reason = str(getattr(exc, "code", type(exc).__name__))
        ranked = pool.evidence
        if plan is not None:
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
                        "selected_count": len(plan.rank_order) if plan is not None else 0,
                        **(
                            {
                                "selector": "complementary_v1",
                                "status": "fallback" if reason else "selected",
                            }
                            if self.product_fallback
                            else {}
                        ),
                        **({"reason": reason} if reason else {}),
                        "admitted_count": len(evidence),
                        "order": "original_relevance",
                    },
                ),
            ),
        )
