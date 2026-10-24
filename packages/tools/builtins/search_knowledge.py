"""Published-snapshot search adapter for the existing hybrid retriever."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.contracts import (
    KnowledgeRetriever,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStrategy,
)
from packages.knowledge.evidence_records import evidence_record, evidence_reference
from packages.knowledge.models import KnowledgeSnapshot
from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSessionFactory
from packages.tools.errors import ToolHandlerError

_MAX_QUERY_LENGTH = 4_000
_MAX_RESULTS = 10


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
                evidence_projection=config.get("evidence_projection"),
            )
        )
    return queries


async def search_knowledge(
    context: ToolExecutionContext,
    definition: ToolDefinition,
    arguments: Mapping[str, Any],
    session_factory: ToolSessionFactory | None,
    *,
    retriever: KnowledgeRetriever | None,
) -> dict[str, Any]:
    if session_factory is None or retriever is None:
        raise ToolHandlerError("TOOL_EXECUTION_FAILED", "The knowledge search is unavailable.")
    text = arguments.get("query")
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_QUERY_LENGTH:
        raise ToolHandlerError("SEARCH_INVALID_ARGUMENT", "The search query is invalid.")
    limit = arguments.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULTS:
        raise ToolHandlerError("SEARCH_INVALID_ARGUMENT", "The result limit is invalid.")

    async with session_factory() as session:
        queries = await _snapshot_queries(session, context, definition, text=text, limit=limit)

    evidence = []
    for query in queries:
        result = await retriever.retrieve_with_trace(_retrieval_context(context), query)
        if not isinstance(result, RetrievalResult):
            raise ToolHandlerError(
                "TOOL_EXECUTION_FAILED", "The knowledge search returned an invalid result."
            )
        for item in result.evidence:
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
    return {
        "evidence_refs": [reference for _, reference in evidence[:limit]],
        "results": [
            evidence_record(
                item,
                snapshot_id=(
                    reference["snapshot_id"]
                    if definition.retrieval_config.get("evidence_projection") == "compact_v1"
                    else None
                ),
            )
            for item, reference in evidence[:limit]
        ],
    }


__all__ = ["search_knowledge"]
