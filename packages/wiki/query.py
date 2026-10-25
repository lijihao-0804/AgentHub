"""Evidence-preserving Wiki query routing.

This module composes the published Wiki projection with the existing raw
retriever.  It deliberately performs extractive retrieval only: no model is
called and a page summary is never presented as the answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import and_, select

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.contracts import RetrievalQuery
from packages.knowledge.models import DocumentChunk, KnowledgeSnapshotItem
from packages.wiki.models import WikiSourceRevocation

_MODES = frozenset({"WIKI", "RAG", "HYBRID", "AUTO"})
_INTENTS = frozenset({"overview", "fact", "synthesis"})
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TOKEN_PART_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+"
)
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WHITESPACE_RE = re.compile(r"\s+")

_EMPTY_QUERY_ANSWER = "The query must not be empty."
_INSUFFICIENT_ANSWER = "Insufficient evidence to answer from the selected knowledge binding."
_CONFLICT_ANSWER = "Conflicting evidence prevents a safe extractive answer."


class WikiServiceProtocol(Protocol):
    async def validate_binding(
        self,
        session: Any,
        context: WorkspaceExecutionContext,
        kb_id: UUID | str,
        compilation_id: UUID | str,
        source_snapshot_id: UUID | str,
        require_current: bool = True,
    ) -> Any: ...

    async def pages(
        self,
        session: Any,
        context: WorkspaceExecutionContext,
        kb_id: UUID | str,
        compilation_id: UUID | str,
    ) -> list[dict[str, Any]]: ...


PageLoader = Callable[
    [Any, WorkspaceExecutionContext, UUID, UUID],
    Awaitable[list[dict[str, Any]]],
]


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.casefold()).strip()


def _tokens(text: str) -> tuple[str, ...]:
    """Tokenize Latin words and Han bigrams for conservative title matching."""

    tokens: list[str] = []
    for word in _WORD_RE.findall(text.casefold()):
        for part in _TOKEN_PART_RE.findall(word):
            if _HAN_RE.fullmatch(part[0]):
                if len(part) == 1:
                    tokens.append(part)
                else:
                    tokens.extend(part[index : index + 2] for index in range(len(part) - 1))
            else:
                tokens.append(part)
    return tuple(dict.fromkeys(tokens))


def _page_match_score(query: str, title: str) -> float:
    normalized_query = _normalize(query)
    normalized_title = _normalize(title)
    if not normalized_query or not normalized_title:
        return 0.0
    if normalized_query == normalized_title:
        return 1.0

    query_tokens = set(_tokens(normalized_query))
    title_tokens = set(_tokens(normalized_title))
    if not query_tokens or not title_tokens:
        return 0.0

    # A title phrase contained in a natural-language question is stronger than
    # isolated token overlap, but the overlap must still be visible.
    if normalized_title in normalized_query:
        return 0.9
    if normalized_query in normalized_title:
        return 0.85

    overlap = query_tokens & title_tokens
    if len(query_tokens) == 1:
        return 0.65 if overlap else 0.0
    if len(overlap) < 2 or len(overlap) / len(query_tokens) < 0.5:
        return 0.0
    return 0.55 + (0.25 * len(overlap) / len(query_tokens))


def _matched_pages(query: str, pages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for candidate in pages:
        page = dict(candidate)
        page_key = str(page.get("page_key", ""))
        if not page_key or page_key in seen:
            continue
        score = _page_match_score(query, str(page.get("title", "")))
        if score <= 0:
            continue
        seen.add(page_key)
        ranked.append((score, str(page.get("title", "")).casefold(), page))
    ranked.sort(key=lambda item: (-item[0], item[1], str(item[2].get("page_key", ""))))
    return [item[2] for item in ranked]


def _error(code: str, message: str, status_code: int) -> AgentHubError:
    return AgentHubError(code, message, status_code)


def _uuid(value: UUID | str, *, field: str, client_input: bool) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        if client_input:
            raise _error(
                "INVALID_WIKI_BINDING",
                f"The {field} is invalid.",
                400,
            ) from None
        raise _error("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


def _attribute(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _same_uuid(value: Any, expected: UUID) -> bool:
    try:
        return UUID(str(value)) == expected
    except (TypeError, ValueError, AttributeError):
        return False


def _default_service() -> WikiServiceProtocol:
    from packages.wiki.service import WikiService

    return WikiService()


def _binding(
    compilation: Any,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    source_snapshot_id: UUID,
    require_current: bool,
) -> dict[str, Any]:
    actual_compilation_id = _attribute(compilation, "id", compilation_id)
    actual_knowledge_base_id = _attribute(compilation, "knowledge_base_id", knowledge_base_id)
    actual_workspace_id = _attribute(compilation, "workspace_id", workspace_id)
    actual_snapshot_id = _attribute(compilation, "source_snapshot_id", source_snapshot_id)
    status = _attribute(compilation, "status", "PUBLISHED")

    if (
        not _same_uuid(actual_compilation_id, compilation_id)
        or not _same_uuid(actual_knowledge_base_id, knowledge_base_id)
        or not _same_uuid(actual_workspace_id, workspace_id)
        or not _same_uuid(actual_snapshot_id, source_snapshot_id)
        or str(status).upper() != "PUBLISHED"
    ):
        raise _error(
            "WIKI_BINDING_MISMATCH",
            "The Wiki compilation binding is inconsistent.",
            409,
        )

    return {
        "schema_version": 1,
        "workspace_id": str(workspace_id),
        "knowledge_base_id": str(knowledge_base_id),
        "compilation_id": str(compilation_id),
        "compilation_hash": _attribute(compilation, "content_hash"),
        "excluded_revision_ids": _attribute(compilation, "compiler_spec", {}).get(
            "excluded_revision_ids", []
        ),
        "source_snapshot_id": str(source_snapshot_id),
        "source_snapshot_hash": _attribute(compilation, "source_snapshot_hash"),
        "compilation_status": str(status).upper(),
        "require_current": bool(require_current),
        "validated": True,
    }


async def _load_pages(
    service: WikiServiceProtocol,
    loader: PageLoader | None,
    session: Any,
    context: WorkspaceExecutionContext,
    knowledge_base_id: UUID,
    compilation_id: UUID,
) -> list[dict[str, Any]]:
    if loader is not None:
        return list(await loader(session, context, knowledge_base_id, compilation_id))
    return list(await service.pages(session, context, knowledge_base_id, compilation_id))


async def _load_snapshot_chunks(
    session: Any,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    source_snapshot_id: UUID,
    chunk_ids: set[str],
) -> dict[str, DocumentChunk]:
    if not chunk_ids:
        return {}
    statement = (
        select(DocumentChunk)
        .join(
            KnowledgeSnapshotItem,
            and_(
                KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id,
                KnowledgeSnapshotItem.knowledge_base_id == DocumentChunk.knowledge_base_id,
                KnowledgeSnapshotItem.document_id == DocumentChunk.document_id,
                KnowledgeSnapshotItem.document_revision_id == DocumentChunk.document_revision_id,
            ),
        )
        .where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.knowledge_base_id == knowledge_base_id,
            KnowledgeSnapshotItem.snapshot_id == source_snapshot_id,
            DocumentChunk.chunk_id.in_(sorted(chunk_ids)),
            DocumentChunk.document_revision_id.not_in(
                select(WikiSourceRevocation.document_revision_id).where(
                    WikiSourceRevocation.workspace_id == workspace_id,
                    WikiSourceRevocation.knowledge_base_id == knowledge_base_id,
                )
            ),
        )
    )
    rows = await session.scalars(statement)
    return {row.chunk_id: row for row in rows}


async def _validate_wiki_claims(
    session: Any,
    pages: Sequence[Mapping[str, Any]],
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    source_snapshot_id: UUID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunk_ids = {
        str(chunk_id)
        for page in pages
        for claim in (page.get("claims") or [])
        if (isinstance((chunk_id := _attribute(claim, "chunk_id", "")), str) and chunk_id)
    }
    chunks = await _load_snapshot_chunks(
        session,
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        source_snapshot_id=source_snapshot_id,
        chunk_ids=chunk_ids,
    )

    validated_pages: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for source_page in pages:
        page = dict(source_page)
        accepted_claims: list[dict[str, Any]] = []
        for claim_value in page.get("claims") or []:
            quote = _attribute(claim_value, "quote")
            chunk_id = _attribute(claim_value, "chunk_id")
            document_id = _attribute(claim_value, "document_id")
            revision_id = _attribute(claim_value, "document_revision_id")
            if not isinstance(quote, str) or not quote.strip():
                continue
            try:
                claimed_document_id = UUID(str(document_id))
                claimed_revision_id = UUID(str(revision_id))
            except (TypeError, ValueError, AttributeError):
                continue
            chunk = chunks.get(chunk_id)
            if (
                chunk is None
                or chunk.document_id != claimed_document_id
                or chunk.document_revision_id != claimed_revision_id
                or quote not in chunk.text
            ):
                continue

            claim = {
                "page_key": page.get("page_key", ""),
                "topic_key": _normalize(str(page.get("title", ""))),
                "quote": quote,
                "chunk_id": chunk.chunk_id,
                "document_id": str(chunk.document_id),
                "document_revision_id": str(chunk.document_revision_id),
                "locator": dict(chunk.locator or {}),
                "predicate": _attribute(claim_value, "predicate"),
                "value": _attribute(claim_value, "value"),
                "condition": _attribute(claim_value, "condition"),
                "conflict": _attribute(claim_value, "conflict", False) is True,
                "conflict_peers": list(_attribute(claim_value, "conflict_peers", [])),
                "conflict_origin": _attribute(claim_value, "conflict_origin", "BASIC_OR_MODEL"),
            }
            accepted_claims.append(claim)
            evidence.append(
                {
                    **claim,
                    "source": "wiki_claim",
                    "retrieval_score": None,
                    "rerank_score": None,
                }
            )

        page["claims"] = accepted_claims
        page["relations"] = list(page.get("relations") or [])
        validated_pages.append(
            {
                "page_key": str(page.get("page_key", "")),
                "title": str(page.get("title", "")),
                "kind": str(page.get("kind", "")),
                "markdown": str(page.get("markdown", "")),
                "claims": accepted_claims,
                "relations": page["relations"],
            }
        )
    return validated_pages, evidence


def _normalized_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _find_conflicts(evidence: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    marked = [item for item in evidence if item.get("source") == "wiki_claim" and item["conflict"]]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in evidence:
        if item.get("source") != "wiki_claim" or item.get("predicate") is None:
            continue
        predicate = str(item.get("predicate")).casefold()
        condition = _normalized_value(item.get("condition"))
        groups.setdefault(
            (str(item.get("topic_key", item.get("page_key", ""))), predicate, condition), []
        ).append(item)

    conflicting_ids = {id(item) for item in marked}
    for members in groups.values():
        values = {_normalized_value(item.get("value")) for item in members}
        if len(values) > 1:
            conflicting_ids.update(id(item) for item in members)

    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        if id(item) not in conflicting_ids:
            continue
        item["conflict"] = True
        key = (
            str(item.get("chunk_id")),
            str(item.get("quote")),
            _normalized_value(item.get("value")),
        )
        if key in seen:
            continue
        seen.add(key)
        conflicts.append(dict(item))
    return conflicts


async def _validate_raw_evidence(
    session: Any,
    raw_results: Sequence[Any],
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    source_snapshot_id: UUID,
) -> list[dict[str, Any]]:
    candidates: list[tuple[Any, str, str, UUID, UUID]] = []
    chunk_ids: set[str] = set()
    for result in raw_results:
        chunk_id = _attribute(result, "chunk_id")
        document_id = _attribute(result, "document_id")
        revision_id = _attribute(result, "document_revision_id")
        quote = _attribute(result, "text", _attribute(result, "quote"))
        if not all((isinstance(chunk_id, str), chunk_id, isinstance(quote, str))):
            continue
        if not quote.strip():
            continue
        try:
            claimed_document_id = UUID(str(document_id))
            claimed_revision_id = UUID(str(revision_id))
        except (TypeError, ValueError, AttributeError):
            continue
        candidates.append((result, chunk_id, quote, claimed_document_id, claimed_revision_id))
        chunk_ids.add(chunk_id)

    chunks = await _load_snapshot_chunks(
        session,
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        source_snapshot_id=source_snapshot_id,
        chunk_ids=chunk_ids,
    )
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for result, chunk_id, quote, claimed_document_id, claimed_revision_id in candidates:
        chunk = chunks.get(chunk_id)
        if (
            chunk is None
            or chunk.document_id != claimed_document_id
            or chunk.document_revision_id != claimed_revision_id
            or quote not in chunk.text
        ):
            continue
        key = (chunk.chunk_id, quote)
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            {
                "quote": quote,
                "chunk_id": chunk.chunk_id,
                "document_id": str(chunk.document_id),
                "document_revision_id": str(chunk.document_revision_id),
                "locator": dict(chunk.locator or {}),
                "predicate": None,
                "value": None,
                "condition": None,
                "conflict": False,
                "source": "raw_retrieval",
                "retrieval_score": _attribute(result, "retrieval_score"),
                "rerank_score": _attribute(result, "rerank_score"),
            }
        )
    return evidence


def _response(
    *,
    binding: dict[str, Any],
    wiki_pages: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    route: str,
    insufficient_evidence: bool,
    supplemental_required: bool,
    conflicts: list[dict[str, Any]],
    answer: str,
    intent: str,
) -> dict[str, Any]:
    return {
        "binding": binding,
        "wiki_pages": wiki_pages,
        "evidence": evidence,
        "route": route,
        "intent": intent,
        "insufficient_evidence": insufficient_evidence,
        "supplemental_required": supplemental_required,
        "conflicts": conflicts,
        "answer": answer,
    }


async def query_wiki(
    session: Any,
    context: WorkspaceExecutionContext,
    kb_id: UUID | str,
    compilation_id: UUID | str,
    source_snapshot_id: UUID | str,
    query: str,
    mode: str = "AUTO",
    intent: str = "overview",
    raw_retriever: Any | None = None,
    require_current: bool = True,
    *,
    service: WikiServiceProtocol | None = None,
    loader: PageLoader | None = None,
) -> dict[str, Any]:
    """Route a query while preserving an explicit, validated evidence binding.

    ``insufficient_evidence`` reports structural evidence availability only.
    It is not a semantic claim that the returned quotations answer the query.
    """

    if "knowledge_run" not in context.permissions:
        raise _error("FORBIDDEN", "You do not have permission.", 403)
    normalized_mode = str(mode).upper()
    normalized_intent = str(intent).casefold()
    if normalized_mode not in _MODES:
        raise _error("INVALID_WIKI_MODE", "The Wiki query mode is invalid.", 400)
    if normalized_intent not in _INTENTS:
        raise _error("INVALID_WIKI_INTENT", "The Wiki query intent is invalid.", 400)
    if not isinstance(query, str):
        raise _error("INVALID_WIKI_QUERY", "The Wiki query is invalid.", 400)

    workspace_id = _uuid(context.workspace_id, field="workspace_id", client_input=False)
    knowledge_base_id = _uuid(kb_id, field="knowledge_base_id", client_input=True)
    compilation_uuid = _uuid(compilation_id, field="compilation_id", client_input=True)
    source_snapshot_uuid = _uuid(
        source_snapshot_id,
        field="source_snapshot_id",
        client_input=True,
    )

    wiki_service = service or _default_service()
    validator = getattr(wiki_service, "validate_binding", None)
    if not callable(validator):
        raise _error(
            "WIKI_SERVICE_INVALID",
            "The Wiki service does not provide binding validation.",
            503,
        )
    compilation = await validator(
        session,
        context,
        knowledge_base_id,
        compilation_uuid,
        source_snapshot_uuid,
        require_current=require_current,
    )
    binding = _binding(
        compilation,
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        compilation_id=compilation_uuid,
        source_snapshot_id=source_snapshot_uuid,
        require_current=require_current,
    )

    if not query.strip():
        return _response(
            binding=binding,
            wiki_pages=[],
            evidence=[],
            route=normalized_mode,
            insufficient_evidence=True,
            supplemental_required=False,
            conflicts=[],
            answer=_EMPTY_QUERY_ANSWER,
            intent=normalized_intent,
        )

    candidate_pages: list[dict[str, Any]] = []
    matched_pages: list[dict[str, Any]] = []
    wiki_pages: list[dict[str, Any]] = []
    wiki_evidence: list[dict[str, Any]] = []
    if normalized_mode != "RAG":
        candidate_pages = await _load_pages(
            wiki_service,
            loader,
            session,
            context,
            knowledge_base_id,
            compilation_uuid,
        )
        matched_pages = _matched_pages(query, candidate_pages)
        selected = {p["page_key"] for p in matched_pages}
        adjacent = set()
        for page in matched_pages:
            for link in page.get("relations", []):
                target = link if isinstance(link, str) else _attribute(link, "target")
                if isinstance(target, str):
                    adjacent.add(target)
        conflict_neighbors = [
            p
            for p in candidate_pages
            if p["page_key"] in adjacent
            and p["page_key"] not in selected
            and any(c.get("conflict") for c in p["claims"])
        ]
        matched_pages.extend(conflict_neighbors)
        selected.update(p["page_key"] for p in conflict_neighbors)
        peer_chunks = {
            key for p in matched_pages for c in p["claims"] for key in c.get("conflict_peers", [])
        }
        matched_pages.extend(
            p
            for p in candidate_pages
            if p["page_key"] not in selected
            and any(c["chunk_id"] in peer_chunks for c in p["claims"])
        )
        wiki_pages, wiki_evidence = await _validate_wiki_claims(
            session,
            matched_pages,
            workspace_id=workspace_id,
            knowledge_base_id=knowledge_base_id,
            source_snapshot_id=source_snapshot_uuid,
        )

    conflicts = _find_conflicts(wiki_evidence)
    needs_raw = normalized_mode in {"RAG", "HYBRID"} or (
        normalized_mode == "AUTO"
        and (normalized_intent in {"fact", "synthesis"} or not wiki_evidence or bool(conflicts))
    )

    raw_evidence: list[dict[str, Any]] = []
    if needs_raw and raw_retriever is not None:
        retrieval_query = RetrievalQuery(
            text=query,
            knowledge_base_id=str(knowledge_base_id),
            knowledge_snapshot_id=str(source_snapshot_uuid),
        )
        raw_results = await raw_retriever.retrieve(context, retrieval_query)
        raw_evidence = await _validate_raw_evidence(
            session,
            list(raw_results or ()),
            workspace_id=workspace_id,
            knowledge_base_id=knowledge_base_id,
            source_snapshot_id=source_snapshot_uuid,
        )

    evidence = [*wiki_evidence, *raw_evidence]
    conflicts = _find_conflicts(evidence)
    supplemental_required = needs_raw and (raw_retriever is None or not raw_evidence)

    if normalized_mode == "WIKI":
        route = "WIKI"
    elif normalized_mode == "RAG":
        route = "RAG"
    elif normalized_mode == "HYBRID":
        route = "HYBRID"
    elif not matched_pages:
        route = "RAG" if raw_retriever is not None else "AUTO"
    elif needs_raw:
        route = "HYBRID" if raw_retriever is not None else "WIKI"
    else:
        route = "WIKI"

    route_requires_raw = normalized_mode in {"RAG", "HYBRID"} or (
        normalized_mode == "AUTO" and normalized_intent in {"fact", "synthesis"}
    )
    insufficient_evidence = bool(
        not evidence or conflicts or (route_requires_raw and supplemental_required)
    )
    if conflicts:
        answer = _CONFLICT_ANSWER
    elif route_requires_raw and supplemental_required:
        answer = _INSUFFICIENT_ANSWER
    elif evidence:
        answer = str(evidence[0]["quote"])
    else:
        answer = _INSUFFICIENT_ANSWER

    return _response(
        binding=binding,
        wiki_pages=wiki_pages,
        evidence=evidence,
        route=route,
        insufficient_evidence=insufficient_evidence,
        supplemental_required=supplemental_required,
        conflicts=conflicts,
        answer=answer,
        intent=normalized_intent,
    )


__all__ = ["query_wiki"]
