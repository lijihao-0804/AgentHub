"""Unit tests for the Commerce MCP Server.

Nothing here opens a socket: the dataset is scripted and in memory, which is the
whole point of it. The "hang" refund mode is asserted on by configuration only
-- a test that actually waited on it would take ten minutes to tell us something
the mode's own constant already says.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import mcp_types
import pytest

from apps.commerce_mcp import commerce_data, server
from apps.commerce_mcp.commerce_data import (
    CommerceError,
    find_customer_orders,
    find_order,
    find_refunds,
    outstanding_amount,
    reset_issued_refunds,
)


@pytest.fixture(autouse=True)
def _forget_issued_refunds() -> Iterator[None]:
    # issue_refund records into process-wide state, so each test starts from the
    # seeded dataset rather than from whatever the previous test bought.
    reset_issued_refunds()
    yield
    reset_issued_refunds()


def _structured(result: mcp_types.CallToolResult) -> dict[str, Any]:
    assert result.is_error is False
    assert result.structured_content is not None
    return dict(result.structured_content)


def _message(result: mcp_types.CallToolResult) -> str:
    assert result.is_error is True
    return "\n".join(
        block.text for block in result.content if isinstance(block, mcp_types.TextContent)
    )


# -- the catalog -----------------------------------------------------------


def test_the_server_offers_exactly_the_three_tools() -> None:
    assert [tool.name for tool in server.TOOLS] == [
        "get_order",
        "get_refund_status",
        "issue_refund",
    ]


def test_the_two_lookups_are_annotated_read_only() -> None:
    for tool in server.TOOLS:
        if tool.name == server.ISSUE_REFUND:
            continue
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False


def test_issue_refund_is_annotated_destructive_and_not_idempotent() -> None:
    tool = next(tool for tool in server.TOOLS if tool.name == server.ISSUE_REFUND)
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    # Two calls issue two refunds, so a runtime must not retry this after a
    # timeout -- the annotation is what tells it so.
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is False


def test_the_write_tool_says_plainly_that_it_moves_money() -> None:
    tool = next(tool for tool in server.TOOLS if tool.name == server.ISSUE_REFUND)
    assert "move money" in (tool.description or "").lower()


def test_every_tool_declares_both_schemas_closed_to_extra_properties() -> None:
    for tool in server.TOOLS:
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema is not None
        assert tool.output_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "schema", [server.ORDER_SCHEMA, server.REFUND_SCHEMA, server.ORDER_ITEM_SCHEMA]
)
def test_every_output_field_is_required(schema: dict[str, Any]) -> None:
    # A field that may be absent forces a consumer to tell "missing" from
    # "unknown"; the contract says present-and-null instead.
    assert sorted(schema["required"]) == sorted(schema["properties"])


@pytest.mark.parametrize(
    ("schema", "arguments", "expected"),
    [
        (server.GET_ORDER_INPUT_SCHEMA, {"order_ref": "ORD-77310"}, None),
        (server.GET_ORDER_INPUT_SCHEMA, {"order_ref": "ORD-77310", "since": "2026"}, "since"),
        (server.ISSUE_REFUND_INPUT_SCHEMA, {"order_ref": "ORD-1", "max_amount": 5}, "max_amount"),
    ],
)
def test_unknown_arguments_are_refused_not_dropped(
    schema: dict[str, Any], arguments: dict[str, Any], expected: str | None
) -> None:
    if expected is None:
        server._reject_unknown(dict(arguments), schema)
        return
    with pytest.raises(CommerceError) as error:
        server._reject_unknown(dict(arguments), schema)
    assert expected in str(error.value)


@pytest.mark.asyncio
async def test_an_unknown_argument_comes_back_as_a_tool_error() -> None:
    result = await server.call_tool("get_order", {"order_ref": "ORD-77310", "since": "2026"})
    assert "since" in _message(result)


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_an_error_not_a_crash() -> None:
    result = await server.call_tool("cancel_order", {})
    assert "Unknown tool" in _message(result)


# -- get_order -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_order_accepts_an_order_reference() -> None:
    payload = _structured(await server.call_tool("get_order", {"order_ref": "ORD-77310"}))
    assert payload["total"] == 1
    order = payload["orders"][0]
    assert order["customer_ref"] == "CUS-10291"
    assert order["status"] == "DELIVERED"
    assert order["delivered_at"] == "2026-09-02T11:22:00Z"


@pytest.mark.asyncio
async def test_get_order_accepts_a_customer_reference_and_returns_recent_orders_first() -> None:
    payload = _structured(await server.call_tool("get_order", {"customer_ref": "CUS-10291"}))
    assert [order["order_ref"] for order in payload["orders"]] == ["ORD-77288", "ORD-77310"]
    assert payload["total"] == 2


@pytest.mark.asyncio
async def test_get_order_refuses_when_neither_reference_is_given() -> None:
    result = await server.call_tool("get_order", {})
    assert "order_ref or customer_ref" in _message(result)


@pytest.mark.asyncio
async def test_get_order_reports_an_undelivered_order_with_a_null_not_a_blank() -> None:
    payload = _structured(await server.call_tool("get_order", {"order_ref": "ORD-77288"}))
    order = payload["orders"][0]
    assert order["status"] == "SHIPPED"
    assert order["shipped_at"] == "2026-09-15T17:40:00Z"
    assert order["delivered_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"order_ref": "ORD-00000"}, {"customer_ref": "CUS-00000"}],
)
async def test_an_unknown_reference_is_refused_rather_than_invented(
    arguments: dict[str, Any],
) -> None:
    result = await server.call_tool("get_order", arguments)
    assert "exists" in _message(result)
    assert result.structured_content is None


def test_find_order_never_synthesises_a_record() -> None:
    with pytest.raises(CommerceError):
        find_order("ORD-00000")
    with pytest.raises(CommerceError):
        find_customer_orders("CUS-00000")


def test_an_order_record_cannot_be_mutated_through_a_caller() -> None:
    find_order("ORD-77310")["status"] = "CANCELLED"
    assert find_order("ORD-77310")["status"] == "DELIVERED"


# -- get_refund_status: the four cases that must read differently ----------


@pytest.mark.asyncio
async def test_a_progressing_refund_shows_an_issue_date_and_no_settlement() -> None:
    payload = _structured(await server.call_tool("get_refund_status", {"order_ref": "ORD-77310"}))
    refund = payload["refunds"][0]
    assert refund["refund_ref"] == "REF-5501"
    assert refund["status"] == "ISSUED"
    assert refund["issued_at"] == "2026-09-17T16:35:00Z"
    # Not yet settled is the normal state days after issue; the null is what
    # says "in flight" rather than "lost".
    assert refund["settled_at"] is None
    assert refund["failure_reason"] is None


@pytest.mark.asyncio
async def test_a_stuck_refund_shows_an_approval_and_no_issue_date() -> None:
    payload = _structured(await server.call_tool("get_refund_status", {"order_ref": "ORD-77412"}))
    refund = payload["refunds"][0]
    assert refund["refund_ref"] == "REF-5512"
    assert refund["status"] == "APPROVED"
    assert refund["approved_at"] == "2026-08-28T08:20:00Z"
    # No money ever moved, which is why waiting cannot fix this one.
    assert refund["issued_at"] is None
    assert refund["settled_at"] is None


@pytest.mark.asyncio
async def test_a_failed_refund_states_why_it_failed() -> None:
    payload = _structured(await server.call_tool("get_refund_status", {"order_ref": "ORD-77455"}))
    refund = payload["refunds"][0]
    assert refund["refund_ref"] == "REF-5523"
    assert refund["status"] == "FAILED"
    assert refund["issued_at"] is not None
    assert refund["settled_at"] is None
    assert "expired" in (refund["failure_reason"] or "")


@pytest.mark.asyncio
async def test_an_order_with_no_refund_returns_an_empty_list_not_a_placeholder() -> None:
    payload = _structured(await server.call_tool("get_refund_status", {"order_ref": "ORD-77501"}))
    assert payload["order_ref"] == "ORD-77501"
    assert payload["refunds"] == []
    assert payload["total"] == 0


@pytest.mark.asyncio
async def test_get_refund_status_refuses_an_unknown_order() -> None:
    result = await server.call_tool("get_refund_status", {"order_ref": "ORD-00000"})
    assert "ORD-00000" in _message(result)


# -- issue_refund ----------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_refund_succeeds_against_an_outstanding_order() -> None:
    payload = _structured(
        await server.call_tool(
            "issue_refund",
            {
                "order_ref": "ORD-77501",
                "amount": 64.00,
                "currency": "USD",
                "reason": "Item arrived damaged.",
            },
        )
    )
    assert payload["order_ref"] == "ORD-77501"
    assert payload["status"] == "ISSUED"
    assert payload["amount"] == 64.00
    assert payload["issued_at"] is not None


@pytest.mark.asyncio
async def test_an_issued_refund_shows_up_in_the_next_status_read() -> None:
    await server.call_tool(
        "issue_refund",
        {"order_ref": "ORD-77412", "amount": 415.00, "currency": "USD", "reason": "Stalled."},
    )
    payload = _structured(await server.call_tool("get_refund_status", {"order_ref": "ORD-77412"}))
    assert [refund["status"] for refund in payload["refunds"]] == ["APPROVED", "ISSUED"]


@pytest.mark.asyncio
async def test_a_refund_larger_than_the_order_is_refused() -> None:
    result = await server.call_tool(
        "issue_refund",
        {"order_ref": "ORD-77501", "amount": 500.00, "currency": "USD", "reason": "Goodwill."},
    )
    assert "exceeds" in _message(result)


@pytest.mark.asyncio
async def test_a_fully_refunded_order_has_nothing_outstanding() -> None:
    result = await server.call_tool(
        "issue_refund",
        {"order_ref": "ORD-77310", "amount": 1.00, "currency": "USD", "reason": "Again."},
    )
    assert "nothing outstanding" in _message(result)


@pytest.mark.asyncio
async def test_a_currency_the_order_is_not_denominated_in_is_refused() -> None:
    result = await server.call_tool(
        "issue_refund",
        {"order_ref": "ORD-77501", "amount": 10.00, "currency": "EUR", "reason": "Partial."},
    )
    assert "USD" in _message(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, -5, "64.00", True])
async def test_an_unusable_amount_is_refused(amount: Any) -> None:
    result = await server.call_tool(
        "issue_refund",
        {"order_ref": "ORD-77501", "amount": amount, "currency": "USD", "reason": "Partial."},
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_issue_refund_refuses_an_unknown_order_before_touching_money() -> None:
    result = await server.call_tool(
        "issue_refund",
        {"order_ref": "ORD-00000", "amount": 5.00, "currency": "USD", "reason": "Test."},
    )
    assert "ORD-00000" in _message(result)


@pytest.mark.parametrize(
    ("order_ref", "expected"),
    [
        # Issued money counts against the order; approved and failed money did
        # not move, so both orders are still fully refundable.
        ("ORD-77310", 0.0),
        ("ORD-77412", 415.00),
        ("ORD-77455", 129.99),
        ("ORD-77501", 64.00),
    ],
)
def test_only_money_that_moved_reduces_the_outstanding_amount(
    order_ref: str, expected: float
) -> None:
    assert outstanding_amount(order_ref) == expected


def test_seeded_refunds_are_listed_oldest_first() -> None:
    refunds = find_refunds("ORD-77310")
    assert [refund["requested_at"] for refund in refunds] == sorted(
        refund["requested_at"] for refund in refunds
    )


# -- configuration ---------------------------------------------------------


def test_the_refund_mode_defaults_to_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMMERCE_MCP_REFUND_MODE", raising=False)
    assert server._env_refund_mode() == server.REFUND_MODE_SUCCEED


def test_the_hang_mode_is_accepted_without_being_exercised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Configuration only: waiting on the mode would burn ten minutes to learn
    # what HANG_SECONDS already states.
    monkeypatch.setenv("COMMERCE_MCP_REFUND_MODE", "HANG")
    assert server._env_refund_mode() == server.REFUND_MODE_HANG
    assert server.HANG_SECONDS >= 600


@pytest.mark.parametrize("mode", ["succeeed", "fail", "true", " "])
def test_an_unrecognised_refund_mode_stops_the_server(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Silently falling back to "succeed" would turn a deliberate failure drill
    # into a passing run.
    monkeypatch.setenv("COMMERCE_MCP_REFUND_MODE", mode)
    if mode.strip():
        with pytest.raises(SystemExit):
            server._env_refund_mode()
    else:
        assert server._env_refund_mode() == server.REFUND_MODE_SUCCEED


def test_an_unparseable_port_stops_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMERCE_MCP_PORT", "eight thousand")
    with pytest.raises(SystemExit):
        server._env_port()


def test_the_host_and_port_defaults_are_the_documented_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMMERCE_MCP_HOST", raising=False)
    monkeypatch.delenv("COMMERCE_MCP_PORT", raising=False)
    assert server._env_host() == "127.0.0.1"
    assert server._env_port() == 8943


def test_build_server_rejects_an_unknown_mode_at_construction() -> None:
    with pytest.raises(SystemExit):
        server.build_server("pretend")


def test_the_customer_references_match_the_platform_s_string_shape() -> None:
    # The builtin query_customer keys a customer by a bare string customer_ref;
    # an agent must be able to carry one reference across both tools unchanged.
    refs = {order["customer_ref"] for order in commerce_data.ORDERS}
    assert refs == {"CUS-10291", "CUS-10344", "CUS-10388", "CUS-10402"}
    assert all(isinstance(ref, str) for ref in refs)
