"""Tool registration for the Ops MCP Server.

The low-level SDK ``Server`` is used rather than the decorator front end
because the schemas below are a frozen contract with AgentHub: they are written
out literally, not derived from a Python signature that could quietly drift when
a parameter is renamed.

Four of the five tools are READ and are expected to be imported with
``effect=READ / risk=LOW / approval_policy=NEVER``. The fifth,
``rollback_deployment``, changes what is running in production; it is annotated
destructive and belongs behind an approval gate with a timeout. The split is the
reason this server exists — an Incident Investigator that could only read would
never reach the part of the governed path that is worth testing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

import mcp_types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server

from apps.ops_mcp.incident_data import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    METRIC_NAMES,
    MIN_LIMIT,
    IncidentDataError,
    get_commit,
    get_deployments,
    plan_rollback,
    query_logs,
    query_metrics,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "ops-mcp"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8941
DEFAULT_PATH = "/mcp"

QUERY_METRICS = "query_metrics"
QUERY_LOGS = "query_logs"
GET_DEPLOYMENTS = "get_deployments"
GET_COMMIT = "get_commit"
ROLLBACK_DEPLOYMENT = "rollback_deployment"

ROLLBACK_MODE_SUCCEED = "succeed"
ROLLBACK_MODE_HANG = "hang"
ROLLBACK_MODES: tuple[str, ...] = (ROLLBACK_MODE_SUCCEED, ROLLBACK_MODE_HANG)
DEFAULT_ROLLBACK_MODE = ROLLBACK_MODE_SUCCEED

# Long enough that no realistic dispatch timeout outlives it, so the caller is
# always the one that gives up first. A value near the timeout would make the
# test flaky in the one direction that matters.
HANG_SECONDS = 600.0

_TIMESTAMP = {"type": "string", "description": "ISO-8601 timestamp, e.g. 2026-03-14T21:00:00Z"}

QUERY_METRICS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "minLength": 1},
        "metric": {"type": "string", "enum": list(METRIC_NAMES)},
        "from_time": dict(_TIMESTAMP),
        "to_time": dict(_TIMESTAMP),
    },
    "required": ["service", "metric", "from_time", "to_time"],
    "additionalProperties": False,
}

QUERY_LOGS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "minLength": 1},
        "query": {
            "type": "string",
            "description": "A log level (INFO, WARN, ERROR, FATAL) or a substring of the message",
        },
        "from_time": dict(_TIMESTAMP),
        "to_time": dict(_TIMESTAMP),
        "limit": {
            "type": "integer",
            "minimum": MIN_LIMIT,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
    "required": ["service", "from_time", "to_time"],
    "additionalProperties": False,
}

GET_DEPLOYMENTS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "minLength": 1},
        "from_time": dict(_TIMESTAMP),
        "to_time": dict(_TIMESTAMP),
    },
    "required": ["service", "from_time", "to_time"],
    "additionalProperties": False,
}

GET_COMMIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commit_sha": {"type": "string", "minLength": 1},
        "repository": {"type": "string"},
    },
    "required": ["commit_sha"],
    "additionalProperties": False,
}

ROLLBACK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "minLength": 1},
        "target_version": {"type": "string", "minLength": 1},
        "reason": {
            "type": "string",
            "minLength": 1,
            "description": "Why the rollback is being performed. Recorded with the change.",
        },
    },
    "required": ["service", "target_version", "reason"],
    "additionalProperties": False,
}

# Every field is required and nullable where the source may have nothing, so a
# consumer never has to tell "absent" from "unknown".
METRIC_POINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timestamp": {"type": "string"},
        # The scripted series has no gaps, but a metrics backend that lost a
        # scrape reports the minute with no value rather than omitting it, and
        # a schema that could not express that would force a gap to be filled
        # with a number nobody measured.
        "value": {"type": ["number", "null"]},
    },
    "required": ["timestamp", "value"],
    "additionalProperties": False,
}

METRIC_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "min": {"type": ["number", "null"]},
        "max": {"type": ["number", "null"]},
        # Null when the window holds no observation from before the deploy:
        # there is then no "before" to compare against, and saying so is more
        # use than a number that looks like one.
        "baseline": {"type": ["number", "null"]},
        "current": {"type": ["number", "null"]},
    },
    "required": ["min", "max", "baseline", "current"],
    "additionalProperties": False,
}

QUERY_METRICS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "metric": {"type": "string"},
        "unit": {"type": "string"},
        "points": {"type": "array", "items": METRIC_POINT_SCHEMA},
        "summary": METRIC_SUMMARY_SCHEMA,
    },
    "required": ["service", "metric", "unit", "points", "summary"],
    "additionalProperties": False,
}

LOG_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timestamp": {"type": "string"},
        "level": {"type": "string"},
        "service": {"type": "string"},
        "message": {"type": "string"},
        "count": {"type": "integer"},
    },
    "required": ["timestamp", "level", "service", "message", "count"],
    "additionalProperties": False,
}

QUERY_LOGS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        # Echoed back, and null when none was given, so a caller reading a
        # stored result can tell an unfiltered query from a filtered one.
        "query": {"type": ["string", "null"]},
        "total": {"type": "integer"},
        "entries": {"type": "array", "items": LOG_ENTRY_SCHEMA},
    },
    "required": ["service", "query", "total", "entries"],
    "additionalProperties": False,
}

DEPLOYMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deployment_id": {"type": "string"},
        "version": {"type": "string"},
        "commit_sha": {"type": "string"},
        "deployed_at": {"type": "string"},
        "deployed_by": {"type": "string"},
        "status": {"type": "string"},
        "environment": {"type": "string"},
    },
    "required": [
        "deployment_id",
        "version",
        "commit_sha",
        "deployed_at",
        "deployed_by",
        "status",
        "environment",
    ],
    "additionalProperties": False,
}

GET_DEPLOYMENTS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "total": {"type": "integer"},
        "deployments": {"type": "array", "items": DEPLOYMENT_SCHEMA},
    },
    "required": ["service", "total", "deployments"],
    "additionalProperties": False,
}

CHANGED_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "additions": {"type": "integer"},
        "deletions": {"type": "integer"},
        # Null where no diff is carried for the file. An empty string would be
        # read as "the file changed by nothing", which is a different claim.
        "diff": {"type": ["string", "null"]},
    },
    "required": ["path", "additions", "deletions", "diff"],
    "additionalProperties": False,
}

GET_COMMIT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commit_sha": {"type": "string"},
        "repository": {"type": "string"},
        "author": {"type": "string"},
        "committed_at": {"type": "string"},
        "message": {"type": "string"},
        "files_changed": {"type": "array", "items": CHANGED_FILE_SCHEMA},
    },
    "required": [
        "commit_sha",
        "repository",
        "author",
        "committed_at",
        "message",
        "files_changed",
    ],
    "additionalProperties": False,
}

ROLLBACK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "rolled_back_from": {"type": "string"},
        "rolled_back_to": {"type": "string"},
        "deployment_id": {"type": "string"},
        "started_at": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": [
        "service",
        "rolled_back_from",
        "rolled_back_to",
        "deployment_id",
        "started_at",
        "status",
    ],
    "additionalProperties": False,
}

# ``openWorldHint`` is false for all five: the dataset is closed and fixed, so
# the same arguments return the same answer no matter who else is running.
READ_ONLY = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Not idempotent, and deliberately not marked so: a second rollback issued
# after a first one that appeared to fail is a second production change, not a
# replay of the first.
DESTRUCTIVE = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)

TOOLS: tuple[mcp_types.Tool, ...] = (
    mcp_types.Tool(
        name=QUERY_METRICS,
        title="Query metrics",
        description=(
            "Return one service metric as a series of points at one-minute resolution across "
            "the requested window, with min, max, pre-deploy baseline and current value. A "
            "window with no observations returns an empty series, not zeroes."
        ),
        inputSchema=QUERY_METRICS_INPUT_SCHEMA,
        outputSchema=QUERY_METRICS_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=QUERY_LOGS,
        title="Query logs",
        description=(
            "Return a service's log entries inside a window, optionally filtered by log level "
            "or by a substring of the message. Repeated lines are aggregated with a count."
        ),
        inputSchema=QUERY_LOGS_INPUT_SCHEMA,
        outputSchema=QUERY_LOGS_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=GET_DEPLOYMENTS,
        title="Get deployments",
        description=(
            "List deployments of a service inside a window, newest first, with the version and "
            "commit sha each one shipped."
        ),
        inputSchema=GET_DEPLOYMENTS_INPUT_SCHEMA,
        outputSchema=GET_DEPLOYMENTS_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=GET_COMMIT,
        title="Get commit",
        description=(
            "Fetch one commit by its sha, or by an unambiguous prefix of at least seven "
            "characters, with its changed files and their unified diffs."
        ),
        inputSchema=GET_COMMIT_INPUT_SCHEMA,
        outputSchema=GET_COMMIT_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=ROLLBACK_DEPLOYMENT,
        title="Roll back deployment",
        description=(
            "WRITE. Changes what is running in production: redeploys the named service at an "
            "earlier version, replacing the version currently serving live traffic. This is "
            "not a simulation and not a dry run. Requires a reason, which is recorded with "
            "the change."
        ),
        inputSchema=ROLLBACK_INPUT_SCHEMA,
        outputSchema=ROLLBACK_OUTPUT_SCHEMA,
        annotations=DESTRUCTIVE,
    ),
)


def build_server(*, rollback_mode: str = DEFAULT_ROLLBACK_MODE) -> Server[Any]:
    """Wire the four READ tools and the one WRITE tool onto a low-level MCP server."""

    mode = _validate_rollback_mode(rollback_mode)

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=list(TOOLS), nextCursor=None)

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        arguments = dict(params.arguments or {})
        try:
            if params.name == QUERY_METRICS:
                _reject_unknown(arguments, QUERY_METRICS_INPUT_SCHEMA)
                payload = _metrics(arguments)
            elif params.name == QUERY_LOGS:
                _reject_unknown(arguments, QUERY_LOGS_INPUT_SCHEMA)
                payload = _logs(arguments)
            elif params.name == GET_DEPLOYMENTS:
                _reject_unknown(arguments, GET_DEPLOYMENTS_INPUT_SCHEMA)
                payload = _deployments(arguments)
            elif params.name == GET_COMMIT:
                _reject_unknown(arguments, GET_COMMIT_INPUT_SCHEMA)
                payload = _commit(arguments)
            elif params.name == ROLLBACK_DEPLOYMENT:
                _reject_unknown(arguments, ROLLBACK_INPUT_SCHEMA)
                payload = await _rollback(arguments, mode)
            else:
                return _tool_error(f"Unknown tool: {params.name}")
        except IncidentDataError as exc:
            # A question this server cannot answer is an answer it owes the
            # agent, not a reason to drop the connection: a run that is told
            # "no such service" can say so, whereas a dead session leaves it
            # guessing.
            logger.info("ops.tool name=%s status=error", params.name)
            return _tool_error(str(exc))
        except Exception:  # noqa: BLE001 - the server must outlive any one call
            logger.exception("ops.tool name=%s status=unexpected", params.name)
            return _tool_error("The ops server failed to complete this call.")
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text=_summary(params.name, payload))],
            structuredContent=payload,
            isError=False,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Ops MCP Server",
        instructions=(
            "Incident investigation over one service's metrics, logs, deployments and commits, "
            "plus a rollback that changes production. Report failures and empty results "
            "verbatim; never invent a metric point, a log line or a deployment."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _reject_unknown(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Hold the schema to its word.

    Every input schema declares ``additionalProperties: false``. Quietly
    dropping an unknown argument would make that a lie, and the caller would
    believe a filter had been applied that never was. AgentHub validates
    arguments against the frozen schema before dispatch, so in the governed
    path this never fires; a standalone client is not so supervised.
    """

    unknown = sorted(set(arguments) - set(schema.get("properties") or {}))
    if unknown:
        raise IncidentDataError(f"Unknown argument(s): {', '.join(unknown)}.")


def _metrics(arguments: dict[str, Any]) -> dict[str, Any]:
    return query_metrics(
        service=_require_string(arguments.get("service"), "service"),
        metric=_require_string(arguments.get("metric"), "metric"),
        from_time=_require_string(arguments.get("from_time"), "from_time"),
        to_time=_require_string(arguments.get("to_time"), "to_time"),
    )


def _logs(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if query is not None and not isinstance(query, str):
        raise IncidentDataError("query must be a string.")
    return query_logs(
        service=_require_string(arguments.get("service"), "service"),
        from_time=_require_string(arguments.get("from_time"), "from_time"),
        to_time=_require_string(arguments.get("to_time"), "to_time"),
        query=query,
        limit=_optional_int(arguments.get("limit"), "limit"),
    )


def _deployments(arguments: dict[str, Any]) -> dict[str, Any]:
    return get_deployments(
        service=_require_string(arguments.get("service"), "service"),
        from_time=_require_string(arguments.get("from_time"), "from_time"),
        to_time=_require_string(arguments.get("to_time"), "to_time"),
    )


def _commit(arguments: dict[str, Any]) -> dict[str, Any]:
    repository = arguments.get("repository")
    if repository is not None and not isinstance(repository, str):
        raise IncidentDataError("repository must be a string.")
    return get_commit(
        commit_sha=_require_string(arguments.get("commit_sha"), "commit_sha"),
        repository=repository,
    )


async def _rollback(arguments: dict[str, Any], mode: str) -> dict[str, Any]:
    service = _require_string(arguments.get("service"), "service")
    target_version = _require_string(arguments.get("target_version"), "target_version")
    # The reason is required by the schema and validated again here. It is not
    # used to decide anything — it exists so the record of a production change
    # carries why it was made, which is the first question asked afterwards.
    reason = _require_string(arguments.get("reason"), "reason")
    # Arguments are checked before the mode branches, so a malformed request is
    # refused immediately even under "hang": a caller that gets a timeout after
    # ten minutes should never find out afterwards that the version was a typo.
    payload = plan_rollback(
        service=service, target_version=target_version, started_at=datetime.now(UTC)
    )
    if mode == ROLLBACK_MODE_HANG:
        # The remote accepts the rollback and then never answers. The caller
        # will time out, but the rollback may well have been performed on the
        # far side before it gave up -- there is no way from here to tell. That
        # is exactly the case the governed runtime must record as an unknown
        # outcome rather than a failure, because retrying it would be a second
        # production change and reporting it as "did not happen" would be a
        # claim this server never made. This mode exists so that path can be
        # exercised end to end instead of reasoned about.
        logger.warning(
            "ops.rollback service=%s target=%s mode=hang reason=%r", service, target_version, reason
        )
        await asyncio.sleep(HANG_SECONDS)
    logger.warning(
        "ops.rollback service=%s target=%s status=%s reason=%r",
        service,
        target_version,
        payload["status"],
        reason,
    )
    return payload


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IncidentDataError(f"{field} is required and must be a non-empty string.")
    return value


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise IncidentDataError(f"{field} must be an integer.")
    return value


def _summary(tool_name: str, payload: dict[str, Any]) -> str:
    """A one-line text block beside the structured content.

    Kept short on purpose: the series, the log page and the diff belong in the
    structured payload, not restated in a model's context. It also says nothing
    interpretive — "the pool is exhausted" is a conclusion the investigation is
    supposed to reach from the data, not one handed to it here.
    """

    if tool_name == QUERY_METRICS:
        return (
            f"{len(payload['points'])} point(s) of {payload['metric']} "
            f"({payload['unit']}) for {payload['service']}."
        )
    if tool_name == QUERY_LOGS:
        return (
            f"{len(payload['entries'])} log entr(ies) returned out of {payload['total']} "
            f"match(es) for {payload['service']}."
        )
    if tool_name == GET_DEPLOYMENTS:
        return f"{payload['total']} deployment(s) of {payload['service']} in the window."
    if tool_name == GET_COMMIT:
        return (
            f"Commit {payload['commit_sha'][:8]} in {payload['repository']} "
            f"touching {len(payload['files_changed'])} file(s)."
        )
    return (
        f"{payload['service']} rolled back from {payload['rolled_back_from']} to "
        f"{payload['rolled_back_to']} ({payload['status']})."
    )


def _tool_error(message: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(text=message)], isError=True)


def create_app(
    *,
    host: str | None = None,
    rollback_mode: str | None = None,
    path: str | None = None,
) -> Any:
    """Build the Starlette app serving this server over Streamable HTTP."""

    server = build_server(
        rollback_mode=rollback_mode if rollback_mode is not None else _env_rollback_mode()
    )
    return server.streamable_http_app(
        streamable_http_path=path or DEFAULT_PATH,
        host=host or _env_host(),
    )


def _env_host() -> str:
    return os.environ.get("OPS_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def _env_port() -> int:
    raw = os.environ.get("OPS_MCP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        # A typo in an env var should not silently move the port a client is
        # configured against.
        raise SystemExit(f"OPS_MCP_PORT is not a port number: {raw!r}") from None


def _env_rollback_mode() -> str:
    return _validate_rollback_mode(
        os.environ.get("OPS_MCP_ROLLBACK_MODE", DEFAULT_ROLLBACK_MODE).strip()
        or DEFAULT_ROLLBACK_MODE
    )


def _validate_rollback_mode(mode: str) -> str:
    """Refuse a rollback mode this server does not implement.

    Falling back to "succeed" on an unrecognised value would be the worst of
    the options: someone who meant to arm the hang path would get a server that
    quietly answers every rollback instead, and would read the resulting green
    run as evidence the timeout path works.
    """

    normalized = mode.strip().lower()
    if normalized not in ROLLBACK_MODES:
        raise SystemExit(
            f"OPS_MCP_ROLLBACK_MODE is not a known mode: {mode!r}. "
            f"Known mode(s): {', '.join(ROLLBACK_MODES)}."
        )
    return normalized


def main() -> None:
    """Run the server over Streamable HTTP until interrupted."""

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = _env_host()
    port = _env_port()
    mode = _env_rollback_mode()
    if mode != DEFAULT_ROLLBACK_MODE:
        # Worth a line in the log. A server that never answers a rollback looks
        # identical to a broken one from the client side, and whoever reads the
        # log afterwards should not have to guess which it was.
        logger.warning("ops.rollback_mode mode=%s (rollback calls will not return)", mode)
    app = create_app(host=host, rollback_mode=mode, path=DEFAULT_PATH)
    logger.info("ops.serve host=%s port=%s path=%s", host, port, DEFAULT_PATH)
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = [
    "CHANGED_FILE_SCHEMA",
    "DEFAULT_HOST",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "DEFAULT_ROLLBACK_MODE",
    "DEPLOYMENT_SCHEMA",
    "GET_COMMIT",
    "GET_COMMIT_INPUT_SCHEMA",
    "GET_COMMIT_OUTPUT_SCHEMA",
    "GET_DEPLOYMENTS",
    "GET_DEPLOYMENTS_INPUT_SCHEMA",
    "GET_DEPLOYMENTS_OUTPUT_SCHEMA",
    "HANG_SECONDS",
    "LOG_ENTRY_SCHEMA",
    "METRIC_POINT_SCHEMA",
    "METRIC_SUMMARY_SCHEMA",
    "QUERY_LOGS",
    "QUERY_LOGS_INPUT_SCHEMA",
    "QUERY_LOGS_OUTPUT_SCHEMA",
    "QUERY_METRICS",
    "QUERY_METRICS_INPUT_SCHEMA",
    "QUERY_METRICS_OUTPUT_SCHEMA",
    "ROLLBACK_DEPLOYMENT",
    "ROLLBACK_INPUT_SCHEMA",
    "ROLLBACK_MODES",
    "ROLLBACK_MODE_HANG",
    "ROLLBACK_MODE_SUCCEED",
    "ROLLBACK_OUTPUT_SCHEMA",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOLS",
    "build_server",
    "create_app",
    "main",
]
