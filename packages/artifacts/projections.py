"""Turning tool results into artifact content, one tool family at a time.

Every projection here obeys the same rule, and it is the rule the whole
artifact design rests on: **the model never writes an artifact**. A projection
reads a tool result, copies fields out of it, and stamps each one with the call
it came from. Nothing it produces can be a sentence the model composed, because
there is no code path that would put one there.

That also bounds what a projection is allowed to do. Copying a deployment's
timestamp into a timeline is recording. Deciding which deployment caused the
incident is investigation, and investigation belongs to the agent and to the
person reading the thread, not to a function that runs on every tool call. So a
metric series becomes "this is what the numbers were", never "this is when it
broke" -- the second claim would be this module inventing a finding and dressing
it as provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts import schemas
from packages.artifacts.research import build_search_content, is_search_tool

MAX_TITLE_LENGTH = 300
# One log query can return more lines than a timeline should hold. The cap is
# the schema's, applied here so the artifact is refused for being wrong rather
# than for being long.
MAX_TIMELINE_ENTRIES = schemas.MAX_ENTRIES


@dataclass(frozen=True, slots=True)
class BuiltArtifact:
    """One artifact a projection wants written, before validation."""

    type: str
    title: str
    content: dict[str, Any]


def tool_tail(tool_identity: str) -> str:
    """The logical tool name, with the workspace's chosen prefix removed.

    An imported MCP tool keeps a workspace-chosen identity, so matching on the
    trailing segment is the only match that survives a workspace renaming its
    connection.
    """

    return tool_identity.rsplit(".", 1)[-1]


def _structured(call: RecordedToolCall) -> dict[str, Any] | None:
    structured = call.data.get("structured_content")
    return structured if isinstance(structured, dict) else None


def _provenance(call: RecordedToolCall, run_id: UUID) -> dict[str, Any]:
    # Written here, never read from the payload: a remote server does not get
    # to claim which call its data arrived on.
    return {
        "run_id": str(run_id),
        "tool_call_id": call.tool_call_id,
        "tool_identity": call.tool_identity,
        "step_sequence": call.step_sequence,
    }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


# --------------------------------------------------------------------------
# research


def project_paper_search(call: RecordedToolCall, run_id: UUID) -> BuiltArtifact | None:
    if not is_search_tool(call.tool_identity):
        return None
    built = build_search_content(call, run_id=run_id)
    if built is None:
        return None
    title, content = built
    return BuiltArtifact(schemas.PAPER_SEARCH, title, content)


# --------------------------------------------------------------------------
# incident


def _metric_entries(structured: dict[str, Any], provenance: dict[str, Any]) -> list[dict[str, Any]]:
    points = structured.get("points")
    if not isinstance(points, list) or not points:
        # A window with no data is a real answer, but it is not an event, and a
        # timeline entry saying nothing happened at no particular time would be
        # noise in the one view that has to stay readable.
        return []
    first = points[0] if isinstance(points[0], dict) else {}
    at = _text(first.get("timestamp"))
    if at is None:
        return []
    metric = _text(structured.get("metric")) or "metric"
    service = _text(structured.get("service")) or "service"
    unit = _text(structured.get("unit")) or ""
    summary_block = structured.get("summary")
    summary_block = summary_block if isinstance(summary_block, dict) else {}
    baseline = _number(summary_block.get("baseline"))
    current = _number(summary_block.get("current"))
    if baseline is None or current is None:
        headline = f"{metric} on {service}: {len(points)} points"
    else:
        headline = f"{metric} on {service}: {baseline} to {current} {unit}".strip()
    last = points[-1] if isinstance(points[-1], dict) else {}
    detail = (
        f"min {summary_block.get('min')}, max {summary_block.get('max')}, "
        f"window {at} to {_text(last.get('timestamp')) or at}, {len(points)} points"
    )
    return [
        {
            "at": at,
            "kind": "METRIC",
            "summary": headline,
            "detail": detail,
            "provenance": provenance,
        }
    ]


def _log_entries(structured: dict[str, Any], provenance: dict[str, Any]) -> list[dict[str, Any]]:
    entries = structured.get("entries")
    if not isinstance(entries, list):
        return []
    built: list[dict[str, Any]] = []
    for entry in entries[:MAX_TIMELINE_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        at = _text(entry.get("timestamp"))
        message = _text(entry.get("message"))
        if at is None or message is None:
            continue
        level = _text(entry.get("level")) or "LOG"
        service = _text(entry.get("service")) or ""
        count = entry.get("count")
        built.append(
            {
                "at": at,
                "kind": "LOG",
                "summary": f"{level} {service}: {message}".strip(),
                "detail": None if count is None else f"count {count}",
                "provenance": provenance,
            }
        )
    return built


def _deployment_entries(
    structured: dict[str, Any], provenance: dict[str, Any]
) -> list[dict[str, Any]]:
    deployments = structured.get("deployments")
    if not isinstance(deployments, list):
        return []
    built: list[dict[str, Any]] = []
    for deployment in deployments[:MAX_TIMELINE_ENTRIES]:
        if not isinstance(deployment, dict):
            continue
        at = _text(deployment.get("deployed_at"))
        version = _text(deployment.get("version"))
        if at is None or version is None:
            continue
        sha = _text(deployment.get("commit_sha")) or ""
        summary = f"Deployed {version}"
        if sha:
            summary += f" ({sha[:10]})"
        environment = _text(deployment.get("environment"))
        if environment:
            summary += f" to {environment}"
        details = [
            f"{key} {deployment.get(key)}"
            for key in ("deployment_id", "deployed_by", "status")
            if _text(deployment.get(key))
        ]
        built.append(
            {
                "at": at,
                "kind": "DEPLOYMENT",
                "summary": summary,
                "detail": ", ".join(details) or None,
                "provenance": provenance,
            }
        )
    return built


def _commit_entries(structured: dict[str, Any], provenance: dict[str, Any]) -> list[dict[str, Any]]:
    at = _text(structured.get("committed_at"))
    sha = _text(structured.get("commit_sha"))
    if at is None or sha is None:
        return []
    message = _text(structured.get("message")) or ""
    headline = message.splitlines()[0] if message else "(no message)"
    files = structured.get("files_changed")
    paths: list[str] = []
    if isinstance(files, list):
        for changed in files:
            if not isinstance(changed, dict):
                continue
            path = _text(changed.get("path"))
            if path is None:
                continue
            paths.append(f"{path} +{changed.get('additions')}/-{changed.get('deletions')}")
    author = _text(structured.get("author"))
    return [
        {
            "at": at,
            "kind": "COMMIT",
            "summary": f"{sha[:10]} {headline}".strip(),
            # The diff itself is not copied here. It can be long, it is already
            # on the run's tool result, and a timeline is an index of what
            # happened rather than the thing that happened.
            "detail": ", ".join(filter(None, [author, "; ".join(paths) or None])) or None,
            "provenance": provenance,
        }
    ]


def _rollback_entries(
    structured: dict[str, Any], provenance: dict[str, Any]
) -> list[dict[str, Any]]:
    at = _text(structured.get("started_at"))
    target = _text(structured.get("rolled_back_to"))
    if at is None or target is None:
        return []
    service = _text(structured.get("service")) or "service"
    origin = _text(structured.get("rolled_back_from")) or "unknown"
    status = _text(structured.get("status")) or "unknown"
    return [
        {
            "at": at,
            "kind": "ACTION",
            "summary": f"Rolled back {service} from {origin} to {target} ({status})",
            "detail": _text(structured.get("deployment_id")),
            "provenance": provenance,
        }
    ]


_TIMELINE_BUILDERS = {
    "query_metrics": _metric_entries,
    "query_logs": _log_entries,
    "get_deployments": _deployment_entries,
    "get_commit": _commit_entries,
    "rollback_deployment": _rollback_entries,
}


def project_incident_timeline(call: RecordedToolCall, run_id: UUID) -> BuiltArtifact | None:
    tail = tool_tail(call.tool_identity)
    builder = _TIMELINE_BUILDERS.get(tail)
    if builder is None:
        return None
    structured = _structured(call)
    if structured is None:
        return None
    entries = builder(structured, _provenance(call, run_id))
    if not entries:
        return None
    service = _text(structured.get("service"))
    subject = service or _text(call.arguments.get("service")) or "unknown service"
    title = f"Timeline: {tail} ({subject})"[:MAX_TITLE_LENGTH]
    return BuiltArtifact(
        schemas.INCIDENT_TIMELINE,
        title,
        {"incident_ref": None, "service": service, "entries": entries},
    )


# --------------------------------------------------------------------------
# analysis


def project_analysis_query(call: RecordedToolCall, run_id: UUID) -> BuiltArtifact | None:
    if tool_tail(call.tool_identity) != "query_sql":
        return None
    structured = _structured(call)
    if structured is None:
        return None
    sql = _text(structured.get("sql")) or _text(call.arguments.get("sql"))
    if sql is None:
        return None
    columns = structured.get("columns")
    if not isinstance(columns, list):
        return None
    rows = structured.get("rows")
    rows = rows if isinstance(rows, list) else []
    # The first line of the SQL is a poor title and a good one: poor because it
    # is not prose, good because it is what the analyst actually ran and can
    # recognise in a list of twelve queries.
    title = f"Query: {' '.join(sql.split())}"[:MAX_TITLE_LENGTH]
    return BuiltArtifact(
        schemas.ANALYSIS_QUERY_RESULT,
        title,
        {
            "question": None,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": structured.get("row_count"),
            "truncated": bool(structured.get("truncated")),
            "provenance": _provenance(call, run_id),
        },
    )


PROJECTIONS = (
    project_paper_search,
    project_incident_timeline,
    project_analysis_query,
)

__all__ = [
    "PROJECTIONS",
    "BuiltArtifact",
    "project_analysis_query",
    "project_incident_timeline",
    "project_paper_search",
    "tool_tail",
]
