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
INCIDENT_TIMELINE = "incident.timeline"
INCIDENT_REPORT = "incident.report"
ANALYSIS_QUERY_RESULT = "analysis.query_result"
ANALYSIS_FINDING = "analysis.finding"
SUPPORT_HANDOFF = "support.handoff"

# The list splits along one line that matters more than the application each
# type belongs to. ``paper_search``, ``timeline`` and ``query_result`` are
# recorded from tool results and carry provenance per entry, so nothing in them
# can have been written by the model. The other three are assembled by a person
# out of what is already on the thread, which is why they may hold prose.
ARTIFACT_TYPES = frozenset(
    {
        PAPER_SEARCH,
        PAPER_SHORTLIST,
        INCIDENT_TIMELINE,
        INCIDENT_REPORT,
        ANALYSIS_QUERY_RESULT,
        ANALYSIS_FINDING,
        SUPPORT_HANDOFF,
    }
)

ARTIFACT_INVALID = "ARTIFACT_INVALID"
ARTIFACT_TYPE_UNKNOWN = "ARTIFACT_TYPE_UNKNOWN"

MAX_PAPERS = 200
MAX_TEXT = 8_000
MAX_AUTHORS = 200
MAX_ENTRIES = 200
MAX_ITEMS = 50
MAX_ROWS = 500
MAX_COLUMNS = 64
MAX_CELL = 2_000


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


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{field} is required.")
    return str(value)[:MAX_TEXT]


def _text_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        _invalid(f"{field} must be a list of strings.")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _invalid(f"{field} must be a list of strings.")
        items.append(item[:MAX_TEXT])
    return items


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


TIMELINE_KINDS = frozenset({"METRIC", "LOG", "DEPLOYMENT", "COMMIT", "ACTION"})


def validate_timeline_entry(value: Any, *, require_provenance: bool) -> dict[str, Any]:
    """Check one line of an incident timeline.

    ``at`` is kept as the string the tool returned rather than parsed into a
    datetime. The observability sources disagree about precision and about
    whether they say Z or +00:00, and normalizing here would mean this module
    deciding what a timestamp from an unfamiliar server meant. An investigator
    comparing 21:01 to 21:03 does not need that decision made for them.
    """

    if not isinstance(value, dict):
        _invalid("a timeline entry must be an object.")
    kind = value.get("kind")
    if kind not in TIMELINE_KINDS:
        _invalid("kind must be one of " + ", ".join(sorted(TIMELINE_KINDS)) + ".")
    entry = {
        "at": _required_text(value.get("at"), "at")[:64],
        "kind": kind,
        "summary": _required_text(value.get("summary"), "summary"),
        "detail": _optional_text(value.get("detail"), "detail"),
    }
    provenance = value.get("provenance")
    if provenance is not None:
        entry["provenance"] = validate_provenance(provenance)
    elif require_provenance:
        # A recorded timeline is the record of tool calls. An entry with no
        # traceable call is an event nobody observed.
        _invalid("every recorded timeline entry must carry provenance.")
    return entry


def _validate_entries(value: Any, *, require_provenance: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _invalid("entries must be a list.")
    if len(value) > MAX_ENTRIES:
        _invalid("entries holds too many rows.")
    return [validate_timeline_entry(item, require_provenance=require_provenance) for item in value]


def _validate_incident_timeline(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "incident_ref": _optional_text(content.get("incident_ref"), "incident_ref"),
        "service": _optional_text(content.get("service"), "service"),
        "entries": _validate_entries(content.get("entries"), require_provenance=True),
    }


def _validate_incident_report(content: dict[str, Any]) -> dict[str, Any]:
    # Assembled by a person at the end of an investigation, out of entries the
    # thread already holds. Its timeline is a copy for the same reason the
    # shortlist copies papers: the investigation continues and the report must
    # keep saying what it said when it was written.
    return {
        "incident_ref": _optional_text(content.get("incident_ref"), "incident_ref"),
        "severity": _optional_text(content.get("severity"), "severity"),
        "summary": _required_text(content.get("summary"), "summary"),
        "impact": _optional_text(content.get("impact"), "impact"),
        "timeline": _validate_entries(content.get("timeline"), require_provenance=False),
        "root_cause": _optional_text(content.get("root_cause"), "root_cause"),
        "actions_taken": _text_list(content.get("actions_taken"), "actions_taken"),
        "remaining_risks": _text_list(content.get("remaining_risks"), "remaining_risks"),
    }


def _validate_rows(value: Any) -> list[list[Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        _invalid("rows must be a list.")
    if len(value) > MAX_ROWS:
        _invalid("rows holds too many rows.")
    rows: list[list[Any]] = []
    for row in value:
        if not isinstance(row, list):
            _invalid("each row must be a list.")
        if len(row) > MAX_COLUMNS:
            _invalid("a row has too many columns.")
        cells: list[Any] = []
        for cell in row:
            if cell is None or isinstance(cell, bool | int | float):
                cells.append(cell)
            elif isinstance(cell, str):
                cells.append(cell[:MAX_CELL])
            else:
                # A nested object in a cell means the tool returned something
                # this is not a table of, and rendering it would be a guess.
                _invalid("a cell must be a string, number, boolean or null.")
        rows.append(cells)
    return rows


def _validate_analysis_query_result(content: dict[str, Any]) -> dict[str, Any]:
    columns = _text_list(content.get("columns"), "columns")
    rows = _validate_rows(content.get("rows"))
    for row in rows:
        if len(row) != len(columns):
            _invalid("every row must have one cell per column.")
    return {
        "question": _optional_text(content.get("question"), "question"),
        "sql": _required_text(content.get("sql"), "sql"),
        "columns": columns,
        "rows": rows,
        "row_count": _optional_int(content.get("row_count"), "row_count"),
        "truncated": bool(content.get("truncated")),
        "provenance": validate_provenance(content.get("provenance")),
    }


def _validate_analysis_finding(content: dict[str, Any]) -> dict[str, Any]:
    # The conclusion is prose because a person wrote it; the queries beneath it
    # are not, and each carries the provenance of the call that produced it.
    raw_queries = content.get("queries")
    if raw_queries is None:
        raw_queries = []
    if not isinstance(raw_queries, list) or len(raw_queries) > MAX_ITEMS:
        _invalid("queries must be a list.")
    queries = []
    for item in raw_queries:
        if not isinstance(item, dict):
            _invalid("each query must be an object.")
        queries.append(_validate_analysis_query_result(item))
    return {
        "question": _required_text(content.get("question"), "question"),
        "conclusion": _required_text(content.get("conclusion"), "conclusion"),
        "evidence": _text_list(content.get("evidence"), "evidence"),
        "queries": queries,
    }


def _validate_support_handoff(content: dict[str, Any]) -> dict[str, Any]:
    # The point of the handoff is that the human does not start over, so the
    # three list fields are what was actually done, not a restatement of the
    # problem. ``recommended_action`` is required: a handoff that reaches a
    # person with no suggested next step has moved the work without advancing
    # it, which is the failure mode escalation is supposed to avoid.
    return {
        "customer_ref": _optional_text(content.get("customer_ref"), "customer_ref"),
        "case_ref": _optional_text(content.get("case_ref"), "case_ref"),
        "problem": _required_text(content.get("problem"), "problem"),
        "checked": _text_list(content.get("checked"), "checked"),
        "findings": _text_list(content.get("findings"), "findings"),
        "recommended_action": _required_text(
            content.get("recommended_action"), "recommended_action"
        ),
        "reason": _optional_text(content.get("reason"), "reason"),
    }


_VALIDATORS = {
    PAPER_SEARCH: _validate_paper_search,
    PAPER_SHORTLIST: _validate_paper_shortlist,
    INCIDENT_TIMELINE: _validate_incident_timeline,
    INCIDENT_REPORT: _validate_incident_report,
    ANALYSIS_QUERY_RESULT: _validate_analysis_query_result,
    ANALYSIS_FINDING: _validate_analysis_finding,
    SUPPORT_HANDOFF: _validate_support_handoff,
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
    "ANALYSIS_FINDING",
    "ANALYSIS_QUERY_RESULT",
    "ARTIFACT_INVALID",
    "ARTIFACT_TYPES",
    "ARTIFACT_TYPE_UNKNOWN",
    "INCIDENT_REPORT",
    "INCIDENT_TIMELINE",
    "PAPER_SEARCH",
    "PAPER_SHORTLIST",
    "SUPPORT_HANDOFF",
    "TIMELINE_KINDS",
    "validate_artifact_content",
    "validate_paper",
    "validate_provenance",
    "validate_timeline_entry",
]
