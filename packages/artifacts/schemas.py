"""What an artifact of each type is allowed to contain.

The table is generic; the contents are not. Every type has an explicit
validator here and nothing reaches the database without passing one, so
"content is JSONB" never turns into "content is whatever anyone wrote".

The paper record shape is the same contract the literature MCP server
normalizes to. It is restated here rather than imported, because this side has
to keep working if that server is replaced.
"""

from __future__ import annotations

from typing import Any

from packages.core.errors.exceptions import AgentHubError

PAPER_SEARCH = "research.paper_search"
PAPER_SHORTLIST = "research.paper_shortlist"

# v1 uses exactly these two. The base is general; the list is not, and it is
# not meant to grow until something real needs it to.
ARTIFACT_TYPES = frozenset({PAPER_SEARCH, PAPER_SHORTLIST})

ARTIFACT_INVALID = "ARTIFACT_INVALID"
ARTIFACT_TYPE_UNKNOWN = "ARTIFACT_TYPE_UNKNOWN"

MAX_PAPERS = 200
MAX_TEXT = 8_000
MAX_AUTHORS = 200


def _invalid(message: str) -> None:
    raise AgentHubError(ARTIFACT_INVALID, message, 422)


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _invalid(f"{field} must be a string or null.")
    if len(value) > MAX_TEXT:
        _invalid(f"{field} is too long.")
    return value


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    # bool is an int in Python and would otherwise slip through as 0/1.
    if not isinstance(value, int) or isinstance(value, bool):
        _invalid(f"{field} must be an integer or null.")
    return value


def validate_provenance(value: Any) -> dict[str, Any]:
    """Check the record of where one paper came from.

    Provenance is what turns "do not fabricate" from a request made of the
    model into a property of the data: a paper with no traceable tool call has
    nowhere to be stored.
    """

    if not isinstance(value, dict):
        _invalid("provenance must be an object.")
    run_id = value.get("run_id")
    tool_call_id = value.get("tool_call_id")
    tool_identity = value.get("tool_identity")
    if not isinstance(run_id, str) or not run_id:
        _invalid("provenance.run_id is required.")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        _invalid("provenance.tool_call_id is required.")
    if not isinstance(tool_identity, str) or not tool_identity:
        _invalid("provenance.tool_identity is required.")
    step_sequence = _optional_int(value.get("step_sequence"), "provenance.step_sequence")
    return {
        "run_id": run_id,
        "tool_call_id": tool_call_id,
        "tool_identity": tool_identity,
        "step_sequence": step_sequence,
    }


def validate_paper(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid("a paper must be an object.")
    paper_id = value.get("paper_id")
    if not isinstance(paper_id, str) or not paper_id or len(paper_id) > 256:
        _invalid("paper_id is required.")
    title = value.get("title")
    if not isinstance(title, str) or not title.strip():
        _invalid("title is required.")
    raw_authors = value.get("authors")
    if raw_authors is None:
        raw_authors = []
    if not isinstance(raw_authors, list) or len(raw_authors) > MAX_AUTHORS:
        _invalid("authors must be a list of strings.")
    authors: list[str] = []
    for author in raw_authors:
        if not isinstance(author, str):
            _invalid("authors must be a list of strings.")
        authors.append(author[:512])
    paper = {
        "paper_id": paper_id,
        "title": title[:MAX_TEXT],
        "authors": authors,
        "year": _optional_int(value.get("year"), "year"),
        "venue": _optional_text(value.get("venue"), "venue"),
        "doi": _optional_text(value.get("doi"), "doi"),
        "abstract": _optional_text(value.get("abstract"), "abstract"),
        "url": _optional_text(value.get("url"), "url"),
        "citation_count": _optional_int(value.get("citation_count"), "citation_count"),
    }
    provenance = value.get("provenance")
    if provenance is not None:
        paper["provenance"] = validate_provenance(provenance)
    return paper


def _validate_papers(value: Any, *, require_provenance: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _invalid("papers must be a list.")
    if len(value) > MAX_PAPERS:
        _invalid("papers holds too many entries.")
    papers = [validate_paper(item) for item in value]
    if require_provenance:
        for paper in papers:
            if "provenance" not in paper:
                # A search result is the record of one tool call. A paper in it
                # that cannot be traced to that call did not come from the
                # server, and the only place it could have come from is the
                # model's memory.
                _invalid("every searched paper must carry provenance.")
    return papers


def _validate_paper_search(content: dict[str, Any]) -> dict[str, Any]:
    query = content.get("query")
    if not isinstance(query, str) or not query.strip():
        _invalid("query is required.")
    source = content.get("source")
    if not isinstance(source, str) or not source.strip():
        _invalid("source is required.")
    filters = content.get("filters") or {}
    if not isinstance(filters, dict):
        _invalid("filters must be an object.")
    return {
        "query": query[:MAX_TEXT],
        "source": source[:64],
        "filters": filters,
        "total": _optional_int(content.get("total"), "total"),
        "papers": _validate_papers(content.get("papers"), require_provenance=True),
    }


def _validate_paper_shortlist(content: dict[str, Any]) -> dict[str, Any]:
    # A shortlist is a copy, not a reference: search results can be superseded
    # by a later query, and what the user picked has to survive that.
    return {
        "note": _optional_text(content.get("note"), "note"),
        "papers": _validate_papers(content.get("papers"), require_provenance=False),
    }


_VALIDATORS = {
    PAPER_SEARCH: _validate_paper_search,
    PAPER_SHORTLIST: _validate_paper_shortlist,
}


def validate_artifact_content(artifact_type: str, content: Any) -> dict[str, Any]:
    """Normalize one artifact's content or refuse it."""

    validator = _VALIDATORS.get(artifact_type)
    if validator is None:
        raise AgentHubError(ARTIFACT_TYPE_UNKNOWN, "The artifact type is not supported.", 422)
    if not isinstance(content, dict):
        _invalid("content must be an object.")
    return validator(content)


__all__ = [
    "ARTIFACT_INVALID",
    "ARTIFACT_TYPES",
    "ARTIFACT_TYPE_UNKNOWN",
    "PAPER_SEARCH",
    "PAPER_SHORTLIST",
    "validate_artifact_content",
    "validate_paper",
    "validate_provenance",
]
