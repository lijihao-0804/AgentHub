"""Published-snapshot search adapter for the existing hybrid retriever."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.composition import retrieval_model_identity_mismatches
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import (
    KnowledgeRetriever,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStrategy,
    RetrievedEvidence,
    SectionSelector,
)
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.knowledge.evidence_records import (
    EvidenceAdmission,
    drop_overlapping_records,
    evidence_record,
    evidence_reference,
)
from packages.knowledge.evidence_set_retriever import EvidenceSetKnowledgeRetriever
from packages.knowledge.models import KnowledgeSnapshot
from packages.knowledge.onyx_retriever import OnyxSelectionKnowledgeRetriever
from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSessionFactory
from packages.tools.errors import ToolHandlerError

_MAX_QUERY_LENGTH = 4_000
_MAX_RESULTS = 10
_MAX_FILTER_LENGTH = 256
_MAX_EXCLUDE_IDS = 200
_ALL_SEEN_HINT = (
    "All matching passages were already returned in earlier turns. "
    "Rephrase your query with different terms or filters, or answer based on existing evidence."
)


def _optional_str_filter(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_FILTER_LENGTH:
        raise ToolHandlerError("SEARCH_INVALID_ARGUMENT", f"The {key} filter is invalid.")
    return value.strip()


def _optional_exclude_ids(arguments: Mapping[str, Any]) -> frozenset[str]:
    raw = arguments.get("exclude_chunk_ids")
    if raw is None:
        return frozenset()
    if (
        not isinstance(raw, list)
        or len(raw) > _MAX_EXCLUDE_IDS
        or any(not isinstance(item, str) or not item.strip() for item in raw)
    ):
        raise ToolHandlerError(
            "SEARCH_INVALID_ARGUMENT", "The exclude_chunk_ids filter is invalid."
        )
    return frozenset(item.strip() for item in raw)


def _optional_retrieval_limit(value: Any) -> int | None:
    return None if value is None else min(int(value), 100)


def _retrieval_context(context: ToolExecutionContext) -> WorkspaceExecutionContext:
    # Tool execution is the authorization boundary for this builtin. The retriever's
    # lower-level permission guard is satisfied only for this internal call.
    return context.workspace_context.model_copy(
        update={"permissions": context.workspace_context.permissions | {"knowledge_run"}}
    )


async def _snapshot_queries(
    session: AsyncSession,
    context: ToolExecutionContext,
    definition: ToolDefinition,
    *,
    text: str,
    limit: int,
) -> list[RetrievalQuery]:
    try:
        workspace_id = UUID(context.workspace_id)
    except ValueError:
        raise ToolHandlerError(
            "TOOL_EXECUTION_FAILED", "The workspace context is invalid."
        ) from None
    if definition.retrieval_config.get("knowledge_binding_mode") != "PINNED":
        raise ToolHandlerError(
            "TOOL_REVISION_INVALID", "The published knowledge binding is invalid."
        )
    if not definition.snapshot_refs or any(
        not item.get("snapshot_id") or not item.get("snapshot_hash")
        for item in definition.snapshot_refs
    ):
        raise ToolHandlerError(
            "TOOL_REVISION_INVALID", "The published knowledge binding is invalid."
        )
    queries: list[RetrievalQuery] = []
    for reference in definition.snapshot_refs:
        try:
            snapshot_id = UUID(reference["snapshot_id"])
        except (KeyError, ValueError):
            raise ToolHandlerError(
                "TOOL_REVISION_INVALID", "The published knowledge binding is invalid."
            ) from None
        snapshot = await session.scalar(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.workspace_id == workspace_id,
                KnowledgeSnapshot.id == snapshot_id,
            )
        )
        if snapshot is None or snapshot.content_hash != reference["snapshot_hash"]:
            raise ToolHandlerError(
                "KNOWLEDGE_SNAPSHOT_INTEGRITY_ERROR",
                "The published knowledge snapshot is unavailable.",
            )
        config = definition.retrieval_config
        try:
            strategy = RetrievalStrategy(
                config.get("retrieval_strategy", RetrievalStrategy.HYBRID_RERANK.value)
            )
        except ValueError:
            raise ToolHandlerError(
                "TOOL_REVISION_INVALID", "The published retrieval strategy is invalid."
            ) from None
        queries.append(
            RetrievalQuery(
                text=text,
                knowledge_base_id=str(snapshot.knowledge_base_id),
                knowledge_snapshot_id=str(snapshot.id),
                strategy=strategy,
                dense_top_k=min(int(config.get("dense_top_k", 30)), 100),
                sparse_top_k=min(int(config.get("sparse_top_k", 30)), 100),
                candidate_top_k=min(int(config.get("candidate_top_k", 20)), 100),
                final_top_k=min(limit, int(config.get("final_top_k", limit)), _MAX_RESULTS),
                rerank_top_k=_optional_retrieval_limit(config.get("rerank_top_k")),
                max_chunks_per_document=_optional_retrieval_limit(
                    config.get("max_chunks_per_document")
                ),
                context_budget_bytes=config.get("context_budget_bytes"),
                rerank_method=config.get("rerank_method"),
                evidence_projection=config.get("evidence_projection"),
            )
        )
    return queries


def _budgeted_snapshot_union(
    ranked: list[tuple[RetrievedEvidence, dict[str, str]]],
    *,
    budget: int,
    limit: int,
    compact: bool,
) -> tuple[list[dict], list[dict]]:
    # Keep the highest-ranked snapshot reference for each immutable source chunk.
    sources = {}
    for item, ref in ranked:
        sources.setdefault((item.document_revision_id, item.chunk_id), (item, ref))
    if not compact:
        selected = select_budgeted_records(
            [asdict(item) for item, _ in sources.values()],
            max_bytes=budget,
            max_items=limit,
            evidence_admission=EvidenceAdmission(),
        )
        pairs = [
            sources[(record["document_revision_id"], record["chunk_id"])] for record in selected
        ]
        return [evidence_record(item) for item, _ in pairs], [ref for _, ref in pairs]

    records, references = [], []
    for item, ref in sources.values():
        if len(records) == limit:
            break
        record = evidence_record(item, snapshot_id=ref["snapshot_id"])
        # A union can exceed the compact planner's 100-input limit. Each trial
        # prices the actual deliverable view of at most 10 records, including
        # its citation IDs. Only already-delivered records can suppress a tail.
        proposal = [*records, record]
        plan = assemble_evidence_plan(
            proposal,
            [*references, ref],
            budget=budget,
            estimator=lambda text: len(text.encode("utf-8")),
            citation_prefix="R00001E",
            max_items=limit,
            evidence_admission=EvidenceAdmission(),
        )
        if plan is None:
            return [], []
        if len(plan.registry) == len(proposal):
            records.append(record)
            references.append(ref)
    return records, references


async def search_knowledge(
    context: ToolExecutionContext,
    definition: ToolDefinition,
    arguments: Mapping[str, Any],
    session_factory: ToolSessionFactory | None,
    *,
    retriever: KnowledgeRetriever | None,
    section_selector: SectionSelector | None = None,
) -> dict[str, Any]:
    if session_factory is None or retriever is None:
        raise ToolHandlerError("TOOL_EXECUTION_FAILED", "The knowledge search is unavailable.")
    text = arguments.get("query")
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_QUERY_LENGTH:
        raise ToolHandlerError("SEARCH_INVALID_ARGUMENT", "The search query is invalid.")
    # A ceiling, not a target: the projection drops whatever the evidence
    # budget cannot carry as a complete record (FIX-06).
    limit = arguments.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULTS:
        raise ToolHandlerError("SEARCH_INVALID_ARGUMENT", "The result limit is invalid.")
    kb_filter = _optional_str_filter(arguments, "knowledge_base_id")
    title_filter = _optional_str_filter(arguments, "document_title")
    seen_chunk_ids = context.seen_chunk_ids | _optional_exclude_ids(arguments)
    mismatches = retrieval_model_identity_mismatches(definition.retrieval_config, retriever)
    if mismatches:
        raise ToolHandlerError(
            "KNOWLEDGE_MODEL_IDENTITY_DRIFT",
            "The published retrieval model is not the deployed model.",
        )
    if (
        getattr(definition, "retrieval_config", None)
        and definition.retrieval_config.get("evidence_set_selector") == "onyx_v1"
    ):
        # Version-level opt-in: the wrapped retriever owns the fail-closed
        # fallback to the original ranking when the selector is missing or
        # fails, so an unconfigured deployment degrades to baseline, never
        # to an error the agent would read as "nothing found".
        retriever = OnyxSelectionKnowledgeRetriever(retriever, section_selector)

    complementary = definition.retrieval_config.get("evidence_set_selector") == "complementary_v1"
    if complementary:
        retriever = EvidenceSetKnowledgeRetriever.for_product(
            retriever, context.selector_binding, context.workspace_context
        )
    async with session_factory() as session:
        queries = await _snapshot_queries(session, context, definition, text=text, limit=limit)
    if kb_filter is not None:
        queries = [q for q in queries if q.knowledge_base_id == kb_filter]
        if not queries:
            return {
                "evidence_refs": [],
                "results": [],
                "filter_matched_snapshots": 0,
            }

    evidence = []
    selection_status = []
    for query in queries:
        result = await retriever.retrieve_with_trace(_retrieval_context(context), query)
        if not isinstance(result, RetrievalResult):
            raise ToolHandlerError(
                "TOOL_EXECUTION_FAILED", "The knowledge search returned an invalid result."
            )
        if complementary:
            stage = result.trace.evidence_set
            if stage is None or stage.metadata is None:
                raise ToolHandlerError("TOOL_EXECUTION_FAILED", "Missing selection receipt.")
            selection_status.append({"snapshot_id": query.knowledge_snapshot_id, **stage.metadata})
        for item in result.evidence:
            if title_filter is not None:
                meta = item.metadata if isinstance(item.metadata, Mapping) else {}
                candidates_title = (
                    item.source or "",
                    str(meta.get("document_name") or ""),
                    str(meta.get("document_title") or ""),
                )
                needle = title_filter.lower()
                if not any(needle in candidate.lower() for candidate in candidates_title):
                    continue
            reference = evidence_reference(
                item,
                snapshot_id=query.knowledge_snapshot_id,
                knowledge_base_id=query.knowledge_base_id,
            )
            evidence.append((item, reference))
    evidence.sort(
        key=lambda item: (
            -(
                item[0].rerank_score
                if item[0].rerank_score is not None
                else item[0].retrieval_score
            ),
            -item[0].retrieval_score,
            item[0].document_revision_id,
            item[0].chunk_id,
        )
    )
    already_seen_count = 0
    if seen_chunk_ids:
        unseen_evidence = []
        for item, ref in evidence:
            if item.chunk_id in seen_chunk_ids:
                already_seen_count += 1
            else:
                unseen_evidence.append((item, ref))
        evidence = unseen_evidence
    # Multi-snapshot unions can re-admit fixed-window neighbours whose overlap
    # a higher-ranked record already delivers; dropping them here frees result
    # slots for complementary passages. chunk_id hashes the revision id and
    # content, so one chunk_id always identifies the same passage.
    retrieval_config = getattr(definition, "retrieval_config", None) or {}
    include_snapshot_ids = retrieval_config.get("evidence_projection") == "compact_v1"
    # A shared revision can occur in two pinned snapshots. Keep the winning
    # reference with the record; reselecting by a set of chunk IDs restores both.
    budget = retrieval_config.get("context_budget_bytes")
    if len(queries) > 1 and budget is not None:
        records, references = _budgeted_snapshot_union(
            evidence, budget=budget, limit=limit, compact=include_snapshot_ids
        )
    else:
        records, references = drop_overlapping_records(
            [
                evidence_record(
                    item, snapshot_id=ref["snapshot_id"] if include_snapshot_ids else None
                )
                for item, ref in evidence
            ],
            [ref for _, ref in evidence],
        )
    payload: dict[str, Any] = {
        "evidence_refs": references[:limit],
        "results": records[:limit],
        **({"evidence_selection": selection_status} if complementary else {}),
    }
    if already_seen_count > 0:
        payload["already_seen_count"] = already_seen_count
        if not payload["results"]:
            payload["all_results_already_seen"] = True
            payload["hint"] = _ALL_SEEN_HINT
    return payload


__all__ = ["search_knowledge"]
