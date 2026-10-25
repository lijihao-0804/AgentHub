"""Native Wiki query tool over the version's frozen publication binding.

The handler is deliberately thin: binding verification, the permission bridge,
and envelope projection around ``packages.wiki.query``. Governance (effect,
risk, approval, audit, budget) is the surrounding tool runtime's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.contracts import KnowledgeRetriever
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS
from packages.tools.contracts import ToolDefinition, ToolExecutionContext, ToolSessionFactory
from packages.tools.errors import ToolHandlerError
from packages.wiki.evidence_envelope import (
    EVIDENCE_ORIGIN_WIKI_DERIVED,
    envelope_violation,
)
from packages.wiki.models import WikiCompilation
from packages.wiki.query import query_wiki as _query_wiki

_MAX_QUERY_LENGTH = 4_000
_MAX_RESULTS = 10

_BINDING_FIELDS = ("knowledge_base_id", "binding_mode", "wiki_publication_id", "publication_hash")


def _retrieval_context(context: ToolExecutionContext) -> WorkspaceExecutionContext:
    # Tool execution is the authorization boundary for this builtin. The wiki
    # layer's permission guard is satisfied only for this internal call, the
    # same bridge search_knowledge uses for the retriever.
    return context.workspace_context.model_copy(
        update={"permissions": context.workspace_context.permissions | {"knowledge_run"}}
    )


def _frozen_binding(definition: ToolDefinition) -> dict[str, str]:
    binding = getattr(definition, "wiki_binding", None)
    if not isinstance(binding, Mapping) or not set(_BINDING_FIELDS) <= set(binding):
        raise ToolHandlerError(
            "WIKI_BINDING_INVALID", "The published Wiki binding is missing or invalid."
        )
    frozen = {key: str(binding[key]) for key in _BINDING_FIELDS}
    try:
        UUID(frozen["knowledge_base_id"])
        UUID(frozen["wiki_publication_id"])
    except ValueError:
        raise ToolHandlerError(
            "WIKI_BINDING_INVALID", "The published Wiki binding is invalid."
        ) from None
    return frozen


async def _compilation(
    session: AsyncSession,
    workspace_id: UUID,
    binding: Mapping[str, str],
) -> WikiCompilation:
    """Load the bound publication and verify it still matches its frozen hash."""

    try:
        knowledge_base_id = UUID(binding["knowledge_base_id"])
        publication_id = UUID(binding["wiki_publication_id"])
    except ValueError:
        raise ToolHandlerError(
            "WIKI_BINDING_INVALID", "The published Wiki binding is invalid."
        ) from None
    row = await session.scalar(
        select(WikiCompilation).where(
            WikiCompilation.workspace_id == workspace_id,
            WikiCompilation.knowledge_base_id == knowledge_base_id,
            WikiCompilation.id == publication_id,
        )
    )
    if row is None or row.status != "PUBLISHED" or row.content_hash != binding["publication_hash"]:
        raise ToolHandlerError(
            "WIKI_BINDING_INVALID", "The bound Wiki publication does not match its frozen hash."
        )
    return row


def _record(item: Mapping[str, Any]) -> dict[str, Any]:
    quote = item.get("quote")
    origin = item.get("origin")
    record: dict[str, Any] = {
        "origin": origin,
        "chunk_id": item["chunk_id"],
        "document_id": item["document_id"],
        "document_revision_id": item["document_revision_id"],
        "locator": dict(item.get("locator") or {}),
        "quote": quote,
        "snippet": str(quote)[:MAX_EVIDENCE_EXCERPT_CHARS],
        "conflict": bool(item.get("conflict", False)),
    }
    if origin == EVIDENCE_ORIGIN_WIKI_DERIVED:
        record.update(
            {
                "publication_id": item["publication_id"],
                "publication_hash": item["publication_hash"],
                "wiki_page_id": item["wiki_page_id"],
                "source_revisions": list(item.get("source_revisions") or []),
            }
        )
    else:
        record.update(
            {
                "retrieval_score": item.get("retrieval_score"),
                "rerank_score": item.get("rerank_score"),
            }
        )
    return record


def _project(result: Mapping[str, Any], compilation: WikiCompilation) -> dict[str, Any]:
    """Reshape the wiki query result into the unified evidence envelope.

    Publication identity travels inside every WIKI_DERIVED record; the
    observation layer's registry projection keeps it out of the model-visible
    payload.
    """

    records: list[dict[str, Any]] = []
    references: list[dict[str, str]] = []
    for item in result.get("evidence") or []:
        if len(records) >= _MAX_RESULTS:
            break
        try:
            record = _record(item)
        except (KeyError, TypeError):
            raise ToolHandlerError(
                "WIKI_ENVELOPE_INVALID", "The Wiki evidence record is malformed."
            ) from None
        reason = envelope_violation(record)
        if reason is not None:
            raise ToolHandlerError(
                "WIKI_ENVELOPE_INVALID", "The Wiki evidence record is missing its envelope."
            ) from None
        records.append(record)
        references.append(
            {
                "snapshot_id": str(compilation.source_snapshot_id),
                "knowledge_base_id": str(compilation.knowledge_base_id),
                "document_revision_id": record["document_revision_id"],
                "chunk_id": record["chunk_id"],
            }
        )
    return {
        "binding": {
            "knowledge_base_id": str(compilation.knowledge_base_id),
            "wiki_publication_id": str(compilation.id),
            "publication_hash": str(compilation.content_hash),
            "source_snapshot_id": str(compilation.source_snapshot_id),
        },
        "route": result.get("route"),
        "insufficient_evidence": bool(result.get("insufficient_evidence")),
        "supplemental_required": bool(result.get("supplemental_required")),
        "conflict_count": len(result.get("conflicts") or []),
        "results": records,
        "evidence_refs": references,
    }


async def query_wiki(
    context: ToolExecutionContext,
    definition: ToolDefinition,
    arguments: Mapping[str, Any],
    session_factory: ToolSessionFactory | None,
    *,
    retriever: KnowledgeRetriever | None = None,
) -> dict[str, Any]:
    """Run the routed wiki query against the version's frozen publication."""

    if session_factory is None:
        raise ToolHandlerError("TOOL_EXECUTION_FAILED", "The Wiki query is unavailable.")
    text = arguments.get("query")
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_QUERY_LENGTH:
        raise ToolHandlerError("WIKI_QUERY_INVALID_ARGUMENT", "The Wiki query is invalid.")
    mode = arguments.get("mode")
    if mode is not None and (
        not isinstance(mode, str) or mode.upper() not in {"WIKI", "RAG", "HYBRID", "AUTO"}
    ):
        raise ToolHandlerError("WIKI_QUERY_INVALID_ARGUMENT", "The Wiki query mode is invalid.")
    intent = arguments.get("intent")
    if intent is not None and (
        not isinstance(intent, str) or intent.casefold() not in {"overview", "fact", "synthesis"}
    ):
        raise ToolHandlerError("WIKI_QUERY_INVALID_ARGUMENT", "The Wiki query intent is invalid.")

    binding = _frozen_binding(definition)
    try:
        workspace_id = UUID(context.workspace_id)
    except ValueError:
        raise ToolHandlerError(
            "TOOL_EXECUTION_FAILED", "The workspace context is invalid."
        ) from None

    async with session_factory() as session:
        compilation = await _compilation(session, workspace_id, binding)
        try:
            result = await _query_wiki(
                session,
                _retrieval_context(context),
                compilation.knowledge_base_id,
                compilation.id,
                compilation.source_snapshot_id,
                text,
                mode=str(mode).upper() if isinstance(mode, str) else "AUTO",
                intent=str(intent).casefold() if isinstance(intent, str) else "overview",
                raw_retriever=retriever,
                # Frozen-run semantics: new sources never enter, revocation
                # still denies at the evidence layer.
                require_current=False,
            )
        except AgentHubError as error:
            if error.code == "WIKI_SOURCE_REVOKED":
                raise ToolHandlerError(
                    "WIKI_BINDING_INVALID",
                    "The bound Wiki publication has a revoked source.",
                ) from None
            raise ToolHandlerError(error.code, error.message) from None
    return _project(result, compilation)


__all__ = ["query_wiki"]
