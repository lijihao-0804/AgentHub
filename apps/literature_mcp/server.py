"""Tool registration for the Literature MCP Server.

The low-level SDK ``Server`` is used rather than the decorator front end
because the schemas below are a frozen contract with AgentHub: they are written
out literally, not derived from a Python signature that could quietly drift when
a parameter is renamed.

Both tools are READ. They are expected to be imported with
``effect=READ / risk=LOW / approval_policy=NEVER``; nothing here is capable of a
side effect, so an approval gate on either would only stall a run.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import mcp_types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server

from apps.literature_mcp.openalex import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_CHARS,
    MIN_LIMIT,
    SOURCE,
    OpenAlexClient,
    OpenAlexError,
    clamp_limit,
    normalize_paper,
    normalize_search_result,
    parse_paper_id,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "literature-mcp"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8931
DEFAULT_PATH = "/mcp"
DEFAULT_MAILTO = "agenthub@example.com"

SEARCH_PAPERS = "search_papers"
GET_PAPER = "get_paper"

SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
        "year_from": {"type": "integer"},
        "year_to": {"type": "integer"},
        "limit": {
            "type": "integer",
            "minimum": MIN_LIMIT,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

GET_PAPER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"paper_id": {"type": "string"}},
    "required": ["paper_id"],
    "additionalProperties": False,
}

# Every field is required and nullable where the source may have nothing, so a
# consumer never has to tell "absent" from "unknown".
PAPER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper_id": {"type": "string"},
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "year": {"type": ["integer", "null"]},
        "venue": {"type": ["string", "null"]},
        "doi": {"type": ["string", "null"]},
        "abstract": {"type": ["string", "null"]},
        "url": {"type": ["string", "null"]},
        "citation_count": {"type": ["integer", "null"]},
    },
    "required": [
        "paper_id",
        "title",
        "authors",
        "year",
        "venue",
        "doi",
        "abstract",
        "url",
        "citation_count",
    ],
    "additionalProperties": False,
}

SEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "query": {"type": "string"},
        "total": {"type": "integer"},
        "papers": {"type": "array", "items": PAPER_SCHEMA},
    },
    "required": ["source", "query", "total", "papers"],
    "additionalProperties": False,
}

GET_PAPER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"source": {"type": "string"}, "paper": PAPER_SCHEMA},
    "required": ["source", "paper"],
    "additionalProperties": False,
}

READ_ONLY = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

TOOLS: tuple[mcp_types.Tool, ...] = (
    mcp_types.Tool(
        name=SEARCH_PAPERS,
        title="Search papers",
        description=(
            "Search scholarly works on OpenAlex by free-text query, optionally bounded by "
            "publication year. Returns normalized paper records."
        ),
        inputSchema=SEARCH_INPUT_SCHEMA,
        outputSchema=SEARCH_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=GET_PAPER,
        title="Get paper",
        description=(
            "Fetch one scholarly work by its prefixed identifier, e.g. "
            "'openalex:W2741809807'. Returns a normalized paper record."
        ),
        inputSchema=GET_PAPER_INPUT_SCHEMA,
        outputSchema=GET_PAPER_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
)


def build_server(client: OpenAlexClient) -> Server[Any]:
    """Wire the two READ tools onto a low-level MCP server."""

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=list(TOOLS), nextCursor=None)

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        arguments = dict(params.arguments or {})
        try:
            if params.name == SEARCH_PAPERS:
                _reject_unknown(arguments, SEARCH_INPUT_SCHEMA)
                payload = await _search(client, arguments)
            elif params.name == GET_PAPER:
                _reject_unknown(arguments, GET_PAPER_INPUT_SCHEMA)
                payload = await _get_paper(client, arguments)
            else:
                return _tool_error(f"Unknown tool: {params.name}")
        except OpenAlexError as exc:
            # An upstream failure is an answer this server owes the agent, not a
            # reason to drop the connection: a run that is told "search failed"
            # can say so, whereas a dead session leaves it guessing.
            logger.info("literature.tool name=%s status=error", params.name)
            return _tool_error(str(exc))
        except Exception:  # noqa: BLE001 - the server must outlive any one call
            logger.exception("literature.tool name=%s status=unexpected", params.name)
            return _tool_error("The literature server failed to complete this call.")
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text=_summary(params.name, payload))],
            structuredContent=payload,
            isError=False,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Literature MCP Server",
        instructions=(
            "Read-only literature search over OpenAlex. Report failures and empty results "
            "verbatim; never invent papers."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _reject_unknown(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Hold the schema to its word.

    Both input schemas declare ``additionalProperties: false``. Quietly
    dropping an unknown argument would make that a lie, and the caller would
    believe a filter had been applied that never was. AgentHub validates
    arguments against the frozen schema before dispatch, so in the governed
    path this never fires; a standalone client is not so supervised.
    """

    unknown = sorted(set(arguments) - set(schema.get("properties") or {}))
    if unknown:
        raise OpenAlexError(f"Unknown argument(s): {', '.join(unknown)}.")


async def _search(client: OpenAlexClient, arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise OpenAlexError("query is required and must be a non-empty string.")
    limit = clamp_limit(_optional_int(arguments.get("limit"), "limit"))
    payload = await client.search_works(
        query,
        year_from=_optional_int(arguments.get("year_from"), "year_from"),
        year_to=_optional_int(arguments.get("year_to"), "year_to"),
        limit=limit,
    )
    return normalize_search_result(payload, query=query.strip(), limit=limit)


async def _get_paper(client: OpenAlexClient, arguments: dict[str, Any]) -> dict[str, Any]:
    paper_id = arguments.get("paper_id")
    if not isinstance(paper_id, str):
        raise OpenAlexError("paper_id is required and must be a string.")
    work = await client.get_work(parse_paper_id(paper_id))
    record = normalize_paper(work)
    if record is None:
        raise OpenAlexError("No usable paper record exists for that id.")
    return {"source": SOURCE, "paper": record}


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenAlexError(f"{field} must be an integer.")
    return value


def _summary(tool_name: str, payload: dict[str, Any]) -> str:
    """A one-line text block beside the structured content.

    Kept short on purpose: the paper list belongs in the structured payload and
    the Artifact built from it, not restated in a model's context.
    """

    if tool_name == SEARCH_PAPERS:
        return (
            f"{len(payload['papers'])} paper(s) returned for {payload['query']!r} "
            f"out of {payload['total']} match(es) on OpenAlex."
        )
    return f"Paper {payload['paper']['paper_id']} returned from OpenAlex."


def _tool_error(message: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(text=message)], isError=True)


def create_app(
    *,
    host: str | None = None,
    mailto: str | None = None,
    api_key: str | None = None,
    path: str | None = None,
) -> Any:
    """Build the Starlette app serving this server over Streamable HTTP."""

    client = OpenAlexClient(
        mailto=mailto if mailto is not None else _env_mailto(),
        api_key=api_key if api_key is not None else _env_api_key(),
    )
    return build_server(client).streamable_http_app(
        streamable_http_path=path or DEFAULT_PATH,
        host=host or _env_host(),
    )


def _env_host() -> str:
    return os.environ.get("LITERATURE_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def _env_port() -> int:
    raw = os.environ.get("LITERATURE_MCP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        # A typo in an env var should not silently move the port a client is
        # configured against.
        raise SystemExit(f"LITERATURE_MCP_PORT is not a port number: {raw!r}") from None


def _env_mailto() -> str:
    return os.environ.get("LITERATURE_MCP_MAILTO", DEFAULT_MAILTO).strip()


def _env_api_key() -> str:
    return os.environ.get("LITERATURE_MCP_API_KEY", "").strip()


def main() -> None:
    """Run the server over Streamable HTTP until interrupted."""

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = _env_host()
    port = _env_port()
    mailto = _env_mailto()
    if mailto == DEFAULT_MAILTO:
        logger.warning(
            "LITERATURE_MCP_MAILTO is unset; OpenAlex will throttle this server's anonymous pool"
        )
    api_key = _env_api_key()
    if api_key:
        # OpenAlex takes the key as a query parameter, and httpx2 logs whole
        # request URLs at INFO. Quieting that logger is cheaper than letting a
        # credential land in a log file nobody expects to be sensitive.
        logging.getLogger("httpx2").setLevel(logging.WARNING)
    else:
        logger.warning(
            "LITERATURE_MCP_API_KEY is unset; requests draw on OpenAlex's shared keyless budget"
        )
    app = create_app(host=host, mailto=mailto, api_key=api_key, path=DEFAULT_PATH)
    logger.info("literature.serve host=%s port=%s path=%s", host, port, DEFAULT_PATH)
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = [
    "GET_PAPER",
    "GET_PAPER_INPUT_SCHEMA",
    "GET_PAPER_OUTPUT_SCHEMA",
    "PAPER_SCHEMA",
    "SEARCH_INPUT_SCHEMA",
    "SEARCH_OUTPUT_SCHEMA",
    "SEARCH_PAPERS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOLS",
    "build_server",
    "create_app",
    "main",
]
