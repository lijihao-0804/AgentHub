"""Tool registration for the Commerce MCP Server.

The low-level SDK ``Server`` is used rather than the decorator front end
because the schemas below are a frozen contract with AgentHub: they are written
out literally, not derived from a Python signature that could quietly drift when
a parameter is renamed.

Two of the three tools are READ and are expected to be imported with
``effect=READ / risk=LOW / approval_policy=NEVER``. ``issue_refund`` is not: it
moves money, and it exists in this demo precisely so there is a tool on the far
side of an approval gate that an operator would actually want to read before
saying yes.

The platform's own ``query_customer``, ``search_knowledge`` and ``create_ticket``
stay where they are. This server supplies only the orders and refunds AgentHub
has no tables for; it shares nothing with them but the shape of a customer
reference.
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

from apps.commerce_mcp.commerce_data import (
    ORDER_STATUSES,
    REFUND_STATUSES,
    CommerceError,
    find_customer_orders,
    find_order,
    find_refunds,
    record_issued_refund,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "commerce-mcp"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8943
DEFAULT_PATH = "/mcp"

GET_ORDER = "get_order"
GET_REFUND_STATUS = "get_refund_status"
ISSUE_REFUND = "issue_refund"

REFUND_MODE_SUCCEED = "succeed"
REFUND_MODE_HANG = "hang"
REFUND_MODES = (REFUND_MODE_SUCCEED, REFUND_MODE_HANG)
DEFAULT_REFUND_MODE = REFUND_MODE_SUCCEED

# Long enough that no realistic post-dispatch timeout outlasts it, so "hang"
# always ends in the caller giving up rather than in a late success.
HANG_SECONDS = 600

GET_ORDER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_ref": {"type": "string", "minLength": 1},
        "customer_ref": {"type": "string", "minLength": 1},
    },
    # JSON Schema cannot say "at least one of these" in a way every MCP client
    # renders usefully, so neither is listed as required here and the handler
    # enforces the rule. anyOf is carried anyway for the clients that do read it.
    "anyOf": [{"required": ["order_ref"]}, {"required": ["customer_ref"]}],
    "additionalProperties": False,
}

GET_REFUND_STATUS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"order_ref": {"type": "string", "minLength": 1}},
    "required": ["order_ref"],
    "additionalProperties": False,
}

ISSUE_REFUND_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_ref": {"type": "string", "minLength": 1},
        "amount": {"type": "number", "exclusiveMinimum": 0},
        "currency": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["order_ref", "amount", "currency", "reason"],
    "additionalProperties": False,
}

ORDER_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sku": {"type": "string"},
        "name": {"type": "string"},
        "quantity": {"type": "integer"},
        "unit_amount": {"type": "number"},
    },
    "required": ["sku", "name", "quantity", "unit_amount"],
    "additionalProperties": False,
}

# Every field is required and nullable where there may be nothing, so a consumer
# never has to tell "absent" from "unknown".
ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_ref": {"type": "string"},
        "customer_ref": {"type": "string"},
        "placed_at": {"type": "string"},
        "status": {"type": "string", "enum": list(ORDER_STATUSES)},
        "currency": {"type": "string"},
        "total_amount": {"type": "number"},
        "items": {"type": "array", "items": ORDER_ITEM_SCHEMA},
        "shipped_at": {"type": ["string", "null"]},
        "delivered_at": {"type": ["string", "null"]},
    },
    "required": [
        "order_ref",
        "customer_ref",
        "placed_at",
        "status",
        "currency",
        "total_amount",
        "items",
        "shipped_at",
        "delivered_at",
    ],
    "additionalProperties": False,
}

# The nullable timestamps are the substance of this record, not decoration. An
# agent can only tell "issued three days ago, banks take three to five business
# days" from "stuck in APPROVED for two weeks" by reading which of them is null.
REFUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "refund_ref": {"type": "string"},
        "requested_at": {"type": ["string", "null"]},
        "approved_at": {"type": ["string", "null"]},
        "issued_at": {"type": ["string", "null"]},
        "settled_at": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": list(REFUND_STATUSES)},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
        "method": {"type": ["string", "null"]},
        "failure_reason": {"type": ["string", "null"]},
    },
    "required": [
        "refund_ref",
        "requested_at",
        "approved_at",
        "issued_at",
        "settled_at",
        "status",
        "amount",
        "currency",
        "method",
        "failure_reason",
    ],
    "additionalProperties": False,
}

GET_ORDER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orders": {"type": "array", "items": ORDER_SCHEMA},
        "total": {"type": "integer"},
    },
    "required": ["orders", "total"],
    "additionalProperties": False,
}

GET_REFUND_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_ref": {"type": "string"},
        "refunds": {"type": "array", "items": REFUND_SCHEMA},
        "total": {"type": "integer"},
    },
    "required": ["order_ref", "refunds", "total"],
    "additionalProperties": False,
}

ISSUE_REFUND_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "refund_ref": {"type": "string"},
        "order_ref": {"type": "string"},
        "status": {"type": "string", "enum": list(REFUND_STATUSES)},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
        "issued_at": {"type": ["string", "null"]},
    },
    "required": ["refund_ref", "order_ref", "status", "amount", "currency", "issued_at"],
    "additionalProperties": False,
}

READ_ONLY = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    # The dataset is scripted and local, so the set of answerable references is
    # closed and an agent that gets "not found" has been told something final.
    openWorldHint=False,
)

DESTRUCTIVE = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    # Two identical calls issue two refunds. Saying so is what stops a governed
    # runtime from retrying this one after a timeout.
    idempotentHint=False,
    openWorldHint=False,
)

TOOLS: tuple[mcp_types.Tool, ...] = (
    mcp_types.Tool(
        name=GET_ORDER,
        title="Get order",
        description=(
            "Look up one order by 'order_ref', or a customer's recent orders by 'customer_ref'. "
            "At least one of the two is required. Returns order records with line items and "
            "shipping timestamps."
        ),
        inputSchema=GET_ORDER_INPUT_SCHEMA,
        outputSchema=GET_ORDER_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=GET_REFUND_STATUS,
        title="Get refund status",
        description=(
            "List every refund on one order with its requested, approved, issued and settled "
            "timestamps. A null timestamp means that step has not happened: read the dates, not "
            "only the status, before telling a customer whether a refund is on track."
        ),
        inputSchema=GET_REFUND_STATUS_INPUT_SCHEMA,
        outputSchema=GET_REFUND_STATUS_OUTPUT_SCHEMA,
        annotations=READ_ONLY,
    ),
    mcp_types.Tool(
        name=ISSUE_REFUND,
        title="Issue refund",
        description=(
            "Move money. This issues a real refund against the order's original payment method "
            "and cannot be undone from here; two calls issue two refunds. Refused if the amount "
            "exceeds what is still outstanding on the order. Use get_refund_status first."
        ),
        inputSchema=ISSUE_REFUND_INPUT_SCHEMA,
        outputSchema=ISSUE_REFUND_OUTPUT_SCHEMA,
        annotations=DESTRUCTIVE,
    ),
)


def build_server(refund_mode: str = DEFAULT_REFUND_MODE) -> Server[Any]:
    """Wire the three tools onto a low-level MCP server."""

    mode = _validate_refund_mode(refund_mode)

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=list(TOOLS), nextCursor=None)

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        return await call_tool(params.name, dict(params.arguments or {}), refund_mode=mode)

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Commerce MCP Server",
        instructions=(
            "Orders and refunds for the Customer Support demo. Report failures and empty results "
            "verbatim; never invent an order or a refund for a reference that was not found. "
            "issue_refund moves money and is not reversible from here."
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def call_tool(
    name: str, arguments: dict[str, Any], *, refund_mode: str = DEFAULT_REFUND_MODE
) -> mcp_types.CallToolResult:
    """Dispatch one tool call and answer with a result, never an exception.

    Lifted out of ``build_server`` so the dispatch behaviour can be exercised
    directly. The contract is the same either way: a failure is an answer this
    server owes the agent, not a reason to drop the connection -- a run that is
    told "no such order" can say so, whereas a dead session leaves it guessing.
    """

    try:
        if name == GET_ORDER:
            _reject_unknown(arguments, GET_ORDER_INPUT_SCHEMA)
            payload = _get_order(arguments)
        elif name == GET_REFUND_STATUS:
            _reject_unknown(arguments, GET_REFUND_STATUS_INPUT_SCHEMA)
            payload = _get_refund_status(arguments)
        elif name == ISSUE_REFUND:
            _reject_unknown(arguments, ISSUE_REFUND_INPUT_SCHEMA)
            payload = await _issue_refund(arguments, refund_mode)
        else:
            return _tool_error(f"Unknown tool: {name}")
    except CommerceError as exc:
        logger.info("commerce.tool name=%s status=error", name)
        return _tool_error(str(exc))
    except Exception:  # noqa: BLE001 - the server must outlive any one call
        logger.exception("commerce.tool name=%s status=unexpected", name)
        return _tool_error("The commerce server failed to complete this call.")
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(text=_summary(name, payload))],
        structuredContent=payload,
        isError=False,
    )


def _reject_unknown(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Hold the schema to its word.

    All three input schemas declare ``additionalProperties: false``. Quietly
    dropping an unknown argument would make that a lie, and on ``issue_refund``
    the lie would be about money: a caller who sent ``max_amount`` and was not
    told it means nothing here believes a cap was applied that never was.
    AgentHub validates arguments against the frozen schema before dispatch, so
    in the governed path this never fires; a standalone client is not so
    supervised.
    """

    unknown = sorted(set(arguments) - set(schema.get("properties") or {}))
    if unknown:
        raise CommerceError(f"Unknown argument(s): {', '.join(unknown)}.")


def _get_order(arguments: dict[str, Any]) -> dict[str, Any]:
    order_ref = arguments.get("order_ref")
    customer_ref = arguments.get("customer_ref")
    if order_ref is None and customer_ref is None:
        raise CommerceError("Either order_ref or customer_ref is required.")
    if order_ref is not None:
        # The order reference is the narrower of the two, so it wins when both
        # are given rather than being silently intersected with the customer.
        orders = [find_order(order_ref)]
    else:
        orders = find_customer_orders(customer_ref)
    return {"orders": orders, "total": len(orders)}


def _get_refund_status(arguments: dict[str, Any]) -> dict[str, Any]:
    order_ref = arguments.get("order_ref")
    if not isinstance(order_ref, str):
        raise CommerceError("order_ref is required and must be a string.")
    order = find_order(order_ref)
    refunds = find_refunds(order["order_ref"])
    # An order with no refund history returns an empty list rather than a
    # synthetic NONE record: "no refund was ever requested" is a fact about the
    # order, and dressing it as a refund invites an agent to describe one.
    return {"order_ref": order["order_ref"], "refunds": refunds, "total": len(refunds)}


async def _issue_refund(arguments: dict[str, Any], refund_mode: str) -> dict[str, Any]:
    order_ref = arguments.get("order_ref")
    if not isinstance(order_ref, str):
        raise CommerceError("order_ref is required and must be a string.")
    currency = arguments.get("currency")
    if not isinstance(currency, str):
        raise CommerceError("currency is required and must be a string.")
    reason = arguments.get("reason")

    if refund_mode == REFUND_MODE_HANG:
        # Never returns within any sane timeout, so the governed runtime's
        # post-dispatch timeout path gets exercised. The point of the mode is
        # what it leaves behind: the remote may well have moved the money
        # before the caller gave up, so the caller must record the outcome as
        # unknown rather than as a failure, and must not retry a call it
        # already annotated as non-idempotent.
        await asyncio.sleep(HANG_SECONDS)

    record = record_issued_refund(
        order_ref,
        amount=arguments.get("amount"),
        currency=currency,
        reason=reason,
        issued_at=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return {
        "refund_ref": record["refund_ref"],
        "order_ref": order_ref.strip(),
        "status": record["status"],
        "amount": record["amount"],
        "currency": record["currency"],
        "issued_at": record["issued_at"],
    }


def _summary(tool_name: str, payload: dict[str, Any]) -> str:
    """A one-line text block beside the structured content.

    Kept short on purpose: the order and refund records belong in the
    structured payload, not restated in a model's context.
    """

    if tool_name == GET_ORDER:
        return f"{payload['total']} order(s) returned."
    if tool_name == GET_REFUND_STATUS:
        return f"{payload['total']} refund(s) on order {payload['order_ref']}."
    return (
        f"Refund {payload['refund_ref']} issued on {payload['order_ref']} "
        f"for {payload['currency']} {payload['amount']:.2f}."
    )


def _tool_error(message: str) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(text=message)], isError=True)


def create_app(
    *,
    host: str | None = None,
    path: str | None = None,
    refund_mode: str | None = None,
) -> Any:
    """Build the Starlette app serving this server over Streamable HTTP."""

    mode = refund_mode if refund_mode is not None else _env_refund_mode()
    return build_server(mode).streamable_http_app(
        streamable_http_path=path or DEFAULT_PATH,
        host=host or _env_host(),
    )


def _env_host() -> str:
    return os.environ.get("COMMERCE_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST


def _env_port() -> int:
    raw = os.environ.get("COMMERCE_MCP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        # A typo in an env var should not silently move the port a client is
        # configured against.
        raise SystemExit(f"COMMERCE_MCP_PORT is not a port number: {raw!r}") from None


def _env_refund_mode() -> str:
    return _validate_refund_mode(
        os.environ.get("COMMERCE_MCP_REFUND_MODE", "").strip() or DEFAULT_REFUND_MODE
    )


def _validate_refund_mode(mode: str) -> str:
    """Accept only a mode this server implements.

    Falling back to "succeed" on a typo would be the worst possible default:
    someone setting the variable is deliberately arranging a failure drill, and
    a drill that quietly succeeds teaches the wrong lesson about the timeout
    path it was meant to exercise.
    """

    cleaned = mode.strip().lower()
    if cleaned not in REFUND_MODES:
        raise SystemExit(
            f"COMMERCE_MCP_REFUND_MODE is not a known mode: {mode!r} "
            f"(expected one of {', '.join(REFUND_MODES)})"
        )
    return cleaned


def main() -> None:
    """Run the server over Streamable HTTP until interrupted."""

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = _env_host()
    port = _env_port()
    refund_mode = _env_refund_mode()
    if refund_mode == REFUND_MODE_HANG:
        logger.warning(
            "commerce.refund_mode=hang; issue_refund will never answer and callers will time out"
        )
    app = create_app(host=host, path=DEFAULT_PATH, refund_mode=refund_mode)
    logger.info("commerce.serve host=%s port=%s path=%s", host, port, DEFAULT_PATH)
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = [
    "DEFAULT_REFUND_MODE",
    "GET_ORDER",
    "GET_ORDER_INPUT_SCHEMA",
    "GET_ORDER_OUTPUT_SCHEMA",
    "GET_REFUND_STATUS",
    "GET_REFUND_STATUS_INPUT_SCHEMA",
    "GET_REFUND_STATUS_OUTPUT_SCHEMA",
    "ISSUE_REFUND",
    "ISSUE_REFUND_INPUT_SCHEMA",
    "ISSUE_REFUND_OUTPUT_SCHEMA",
    "ORDER_SCHEMA",
    "REFUND_MODES",
    "REFUND_SCHEMA",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOLS",
    "build_server",
    "call_tool",
    "create_app",
    "main",
]
