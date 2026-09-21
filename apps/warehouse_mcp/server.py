"""Tool registration for the Warehouse MCP Server.

The low-level SDK ``Server`` is used rather than the decorator front end
because the schemas below are a frozen contract with AgentHub: they are written
out literally, not derived from a Python signature that could quietly drift when
a parameter is renamed.

All four tools are READ, and that is enforced rather than asserted -- see
``sql_guard``. They are expected to be imported with
``effect=READ / risk=LOW / approval_policy=NEVER``; an approval gate on a query
that cannot write would only stall a run.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import mcp_types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server

from apps.warehouse_mcp.sql_guard import MAX_ROW_CAP, SqlGuardError
from apps.warehouse_mcp.warehouse import METRICS, TABLE_NAMES, Warehouse, WarehouseError
from apps.warehouse_mcp.warehouse import metric_definition as lookup_metric

logger = logging.getLogger(__name__)

SERVER_NAME = "warehouse-mcp"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8942
DEFAULT_PATH = "/mcp"

MIN_LIMIT = 1
DEFAULT_LIMIT = 200

GET_METRIC_DEFINITION = "get_metric_definition"
LIST_TABLES = "list_tables"
DESCRIBE_TABLE = "describe_table"
QUERY_SQL = "query_sql"

METRIC_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"metric": {"type": "string", "minLength": 1}},
    "required": ["metric"],
    "additionalProperties": False,
}

# Every field is required and nullable where a metric may genuinely have
# nothing there, so a consumer never has to tell "absent" from "unknown". A
# rate has a denominator; a definition like paying_user has none, and saying so
# with null is more useful than omitting the key.
METRIC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {"type": "string"},
        "display_name": {"type": "string"},
        "definition": {"type": "string"},
        "formula": {"type": ["string", "null"]},
        "numerator": {"type": ["string", "null"]},
        "denominator": {"type": ["string", "null"]},
        "caveats": {"type": ["string", "null"]},
        "source_tables": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "metric",
        "display_name",
        "definition",
        "formula",
        "numerator",
        "denominator",
        "caveats",
        "source_tables",
    ],
    "additionalProperties": False,
}

LIST_TABLES_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

LIST_TABLES_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "row_count": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["name", "row_count", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tables"],
    "additionalProperties": False,
}

DESCRIBE_TABLE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"table": {"type": "string", "minLength": 1}},
    "required": ["table"],
    "additionalProperties": False,
}

COLUMN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "nullable": {"type": "boolean"},
        "description": {"type": "string"},
    },
    "required": ["name", "type", "nullable", "description"],
    "additionalProperties": False,
}

DESCRIBE_TABLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "table": {"type": "string"},
        "description": {"type": "string"},
        "columns": {"type": "array", "items": COLUMN_SCHEMA},
        "row_count": {"type": "integer"},
        # Null rather than an empty object when every column is too
        # high-cardinality to sample usefully.
        "sample_values": {"type": ["object", "null"]},
    },
    "required": ["table", "description", "columns", "row_count", "sample_values"],
    "additionalProperties": False,
}

QUERY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "minLength": 1},
        "limit": {
            "type": "integer",
            "minimum": MIN_LIMIT,
            "maximum": MAX_ROW_CAP,
            "default": DEFAULT_LIMIT,
        },
    },
    "required": ["sql"],
    "additionalProperties": False,
}

QUERY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {
            "type": "array",
            "items": {"type": "array", "items": {"type": ["string", "number", "null"]}},
        },
        "row_count": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "elapsed_ms": {"type": "number"},
    },
    "required": ["sql", "columns", "rows", "row_count", "truncated", "elapsed_ms"],
    "additionalProperties": False,
}

# openWorldHint is False where the literature server sets it True: this server
# answers from one database it seeds itself, so the same call returns the same
# rows rather than whatever an outside platform happens to hold today.
READ_ONLY = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

TOOLS: tuple[mcp_types.Tool, ...] = (
    mcp_types.Tool(
        name=GET_METRIC_DEFINITION,
        title="Get metric definition",
        description=(
            "Look up how the data team defines a business metric: its formula, its "
            "numerator and denominator, the tables it comes from, and the caveats "
            "that make it arguable. Read this before computing a metric by hand."
        ),
        inputSchema=METRIC_INPUT_SCHEMA,
        outputSchema=METRIC_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=LIST_TABLES,
        title="List tables",
        description="List every table in the warehouse with its row count and what it holds.",
        inputSchema=LIST_TABLES_INPUT_SCHEMA,
        outputSchema=LIST_TABLES_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=DESCRIBE_TABLE,
        title="Describe table",
        description=(
            "Describe one table: its columns, their types and meaning, its row count, "
            "and sample values for the low-cardinality columns."
        ),
        inputSchema=DESCRIBE_TABLE_INPUT_SCHEMA,
        outputSchema=DESCRIBE_TABLE_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=QUERY_SQL,
        title="Query SQL",
        description=(
            "Run one read-only SQL statement against the warehouse and return its rows. "
            "Only a single SELECT or WITH ... SELECT is accepted; anything that writes "
            "is rejected. Results are capped by 'limit' and report whether they were cut."
        ),
        inputSchema=QUERY_INPUT_SCHEMA,
        outputSchema=QUERY_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
)


def build_server(warehouse: Warehouse) -> Server[Any]:
    """Wire the four READ tools onto a low-level MCP server."""

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=list(TOOLS), nextCursor=None)

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        arguments = dict(params.arguments or {})
        try:
            if params.name == GET_METRIC_DEFINITION:
                _reject_unknown(arguments, METRIC_INPUT_SCHEMA)
                payload = _get_metric_definition(arguments)
            elif params.name == LIST_TABLES:
                _reject_unknown(arguments, LIST_TABLES_INPUT_SCHEMA)
                payload = warehouse.list_tables()
            elif params.name == DESCRIBE_TABLE:
                _reject_unknown(arguments, DESCRIBE_TABLE_INPUT_SCHEMA)
                payload = _describe_table(warehouse, arguments)
            elif params.name == QUERY_SQL:
                _reject_unknown(arguments, QUERY_INPUT_SCHEMA)
                payload = _query_sql(warehouse, arguments)
            else:
                return _tool_error(f"Unknown tool: {params.name}")
        except SqlGuardError as exc:
            # A refused statement is an answer the agent can act on -- it names
            # the statement kind, so the next attempt can be a SELECT. Raising
            # here instead would drop the session over a recoverable mistake.
            logger.info("warehouse.tool name=%s status=rejected", params.name)
            return _tool_error(str(exc))
        except WarehouseError as exc:
            logger.info("warehouse.tool name=%s status=error", params.name)
            return _tool_error(str(exc))
        except Exception:  # noqa: BLE001 - the server must outlive any one call
            logger.exception("warehouse.tool name=%s status=unexpected", params.name)
            return _tool_error("The warehouse server failed to complete this call.")
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text=_summary(params.name, payload))],
            structuredContent=payload,
            isError=False,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Warehouse MCP Server",
        instructions=(
            "Read-only access to the product analytics warehouse. Look a metric up with "
            "get_metric_definition before computing it, and report query failures and "
            "empty results verbatim; never invent numbers."
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
        raise WarehouseError(f"Unknown argument(s): {', '.join(unknown)}.")


def _get_metric_definition(arguments: dict[str, Any]) -> dict[str, Any]:
    metric = arguments.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        raise WarehouseError("metric is required and must be a non-empty string.")
    return lookup_metric(metric)


def _describe_table(warehouse: Warehouse, arguments: dict[str, Any]) -> dict[str, Any]:
    table = arguments.get("table")
    if not isinstance(table, str) or not table.strip():
        raise WarehouseError("table is required and must be a non-empty string.")
    return warehouse.describe_table(table.strip())


def _query_sql(warehouse: Warehouse, arguments: dict[str, Any]) -> dict[str, Any]:
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise WarehouseError("sql is required and must be a non-empty string.")
    return warehouse.query(sql, clamp_limit(arguments.get("limit")))


def clamp_limit(limit: Any) -> int:
    """Force a row limit into the declared range.

    The input schema already bounds ``limit``, but a schema is the client's
    promise, not this server's guarantee. Clamping rather than rejecting keeps a
    slightly-wrong request useful instead of turning it into a tool error.
    """

    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise WarehouseError("limit must be an integer.")
    return max(MIN_LIMIT, min(MAX_ROW_CAP, limit))


def _summary(tool_name: str, payload: dict[str, Any]) -> str:
    """A one-line text block beside the structured content.

    Kept short on purpose: the rows belong in the structured payload and the
    Artifact built from it, not restated in a model's context.
    """

    if tool_name == GET_METRIC_DEFINITION:
        return f"Definition of {payload['metric']} returned from the metric dictionary."
    if tool_name == LIST_TABLES:
        return f"{len(payload['tables'])} table(s) in the warehouse."
    if tool_name == DESCRIBE_TABLE:
        return (
            f"Table {payload['table']} has {len(payload['columns'])} column(s) "
            f"and {payload['row_count']} row(s)."
        )
    cut = " (truncated)" if payload["truncated"] else ""
    return f"{payload['row_count']} row(s) returned in {payload['elapsed_ms']:.1f} ms{cut}."


def _tool_error(message: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(text=message)], isError=True)


def create_app(
    *, host: str | None = None, database_path: str | None = None, path: str | None = None
) -> Any:
    """Build the Starlette app serving this server over Streamable HTTP.

    The database is seeded here rather than on first query so that a server
    that answered its health check is a server that can answer a question.
    """

    warehouse = Warehouse(database_path or _env_database_path())
    warehouse.build()
    return build_server(warehouse).streamable_http_app(
        streamable_http_path=path or DEFAULT_PATH,
        host=host or _env_host(),
    )


def _env_host() -> str:
    return os.environ.get("WAREHOUSE_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def _env_port() -> int:
    raw = os.environ.get("WAREHOUSE_MCP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        # A typo in an env var should not silently move the port a client is
        # configured against.
        raise SystemExit(f"WAREHOUSE_MCP_PORT is not a port number: {raw!r}") from None


def _env_database_path() -> Path:
    """Where the demo database is written.

    A temp-directory file by default: it is rebuilt from the same seed at every
    start, so nothing of value is kept there and nothing in the repository is
    touched. Overridable for an operator who would rather it lived somewhere
    they can inspect.
    """

    raw = os.environ.get("WAREHOUSE_MCP_DATABASE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(tempfile.gettempdir()) / "agenthub-warehouse-mcp" / "warehouse.sqlite3"


def main() -> None:
    """Run the server over Streamable HTTP until interrupted."""

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = _env_host()
    port = _env_port()
    database_path = _env_database_path()
    app = create_app(host=host, database_path=str(database_path), path=DEFAULT_PATH)
    logger.info("warehouse.seed path=%s tables=%s", database_path, len(TABLE_NAMES))
    logger.info(
        "warehouse.serve host=%s port=%s path=%s metrics=%s", host, port, DEFAULT_PATH, len(METRICS)
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = [
    "DEFAULT_LIMIT",
    "DESCRIBE_TABLE",
    "DESCRIBE_TABLE_INPUT_SCHEMA",
    "DESCRIBE_TABLE_OUTPUT_SCHEMA",
    "GET_METRIC_DEFINITION",
    "LIST_TABLES",
    "LIST_TABLES_INPUT_SCHEMA",
    "LIST_TABLES_OUTPUT_SCHEMA",
    "METRIC_INPUT_SCHEMA",
    "METRIC_OUTPUT_SCHEMA",
    "QUERY_INPUT_SCHEMA",
    "QUERY_OUTPUT_SCHEMA",
    "QUERY_SQL",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOLS",
    "build_server",
    "clamp_limit",
    "create_app",
    "main",
]
