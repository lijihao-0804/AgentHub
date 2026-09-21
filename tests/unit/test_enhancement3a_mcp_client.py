"""Enhancement 3A: the MCP protocol adapter, driven against in-process servers.

Every test here runs a real MCP SDK session — real handshake, real
``tools/list``, real cursor — over an in-memory transport. Nothing touches a
socket, so the suite is deterministic and works offline, while still
exercising the protocol rather than a hand-rolled stand-in for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
import pytest
from mcp import Client
from mcp.server.lowlevel.server import Server
from mcp_types import ListToolsResult, PaginatedRequestParams, Tool, ToolAnnotations

from packages.core.config.settings import Settings
from packages.mcp.client import (
    MCP_CONNECT_FAILED,
    MCP_CONNECT_TIMEOUT,
    MCP_DISCOVERY_INVALID,
    MCP_DISCOVERY_LIMIT_EXCEEDED,
    MCP_DISCOVERY_PAYLOAD_TOO_LARGE,
    MCP_PROTOCOL_ERROR,
    MCP_TARGET_FORBIDDEN,
    MCP_TOOL_SCHEMA_TOO_LARGE,
    McpClientAdapter,
    McpConnectionTarget,
    McpRemoteError,
)
from packages.mcp.models import McpAuthType
from tests.support.settings import declared_settings

pytestmark = pytest.mark.asyncio

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def target(endpoint: str = "https://mcp.example.com/mcp") -> McpConnectionTarget:
    return McpConnectionTarget(
        connection_id=uuid4(), endpoint_url=endpoint, auth_type=McpAuthType.NONE
    )


def settings(**overrides: object) -> Settings:
    # Declared defaults only. A test that drives the adapter's *default*
    # behaviour has to start from the code's own defaults, not from whatever
    # the developer running it has configured locally.
    return declared_settings(testing=True, environment="test", **overrides)


def tool(
    name: str,
    *,
    schema: dict | None = None,
    annotations: ToolAnnotations | None = None,
    description: str | None = None,
):
    return Tool(name=name, description=description or f"{name} tool",
                inputSchema=schema or SCHEMA, annotations=annotations)


def server_with(pages: list[tuple[list[Tool], str | None]]) -> Server:
    """A server whose ``tools/list`` walks the given pages via real cursors."""

    async def on_list_tools(_ctx, params: PaginatedRequestParams | None) -> ListToolsResult:
        cursor = params.cursor if params is not None else None
        index = 0 if cursor is None else int(cursor)
        tools, next_cursor = pages[index]
        return ListToolsResult(tools=tools, nextCursor=next_cursor)

    return Server("fake-mcp", version="9.9.9", on_list_tools=on_list_tools)


def adapter_for(server: Server, **setting_overrides: object) -> McpClientAdapter:
    @asynccontextmanager
    async def factory(_target, _secret, _observation) -> AsyncIterator[Client]:
        async with Client(server, cache=None) as client:
            yield client

    return McpClientAdapter(settings=settings(**setting_overrides), client_factory=factory)


def failing_adapter(exc: BaseException, **setting_overrides: object) -> McpClientAdapter:
    @asynccontextmanager
    async def factory(_target, _secret, _observation) -> AsyncIterator[Client]:
        raise exc
        yield  # pragma: no cover - unreachable, keeps the generator a generator

    return McpClientAdapter(settings=settings(**setting_overrides), client_factory=factory)


async def test_test_connection_reports_healthy_with_server_metadata() -> None:
    outcome = await adapter_for(server_with([([tool("search")], None)])).test_connection(
        target(), None
    )

    assert outcome.status == "healthy"
    assert outcome.failure is None
    assert outcome.server is not None
    assert outcome.server.server_name == "fake-mcp"
    assert outcome.server.server_version == "9.9.9"
    assert outcome.latency_ms >= 0


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx2.ConnectError("boom"), MCP_CONNECT_FAILED),
        (httpx2.ConnectTimeout("boom"), MCP_CONNECT_TIMEOUT),
        (httpx2.RemoteProtocolError("boom"), MCP_PROTOCOL_ERROR),
        (McpRemoteError(MCP_TARGET_FORBIDDEN), MCP_TARGET_FORBIDDEN),
    ],
)
async def test_test_connection_normalizes_remote_failures(raised, expected) -> None:
    outcome = await failing_adapter(raised).test_connection(target(), None)

    assert outcome.status == "unavailable"
    assert outcome.server is None
    assert outcome.failure is not None
    assert outcome.failure.failure_code == expected


async def test_test_connection_failure_never_carries_remote_detail() -> None:
    secret_ish = "https://user:tok-abcdef@internal.example/mcp body=<stack trace>"

    outcome = await failing_adapter(httpx2.ConnectError(secret_ish)).test_connection(target(), None)

    assert outcome.failure is not None
    assert outcome.failure.failure_code == MCP_CONNECT_FAILED
    assert "tok-abcdef" not in repr(outcome)
    assert "stack trace" not in repr(outcome)


async def test_discover_tools_single_page_preserves_server_order() -> None:
    page = [tool("zeta"), tool("alpha"), tool("mid")]

    outcome = await adapter_for(server_with([(page, None)])).discover_tools(target(), None)

    assert [entry.name for entry in outcome.tools] == ["zeta", "alpha", "mid"]
    assert outcome.tools[0].input_schema == SCHEMA
    assert outcome.server is not None and outcome.server.server_name == "fake-mcp"


async def test_discover_tools_follows_pagination_across_pages() -> None:
    pages = [
        ([tool("a"), tool("b")], "1"),
        ([tool("c")], "2"),
        ([tool("d")], None),
    ]

    outcome = await adapter_for(server_with(pages)).discover_tools(target(), None)

    assert [entry.name for entry in outcome.tools] == ["a", "b", "c", "d"]


async def test_discover_tools_refuses_to_page_past_the_tool_budget() -> None:
    pages = [([tool("a"), tool("b")], "1"), ([tool("c")], None)]

    with pytest.raises(McpRemoteError) as excinfo:
        await adapter_for(server_with(pages), mcp_discovery_max_tools=2).discover_tools(
            target(), None
        )

    assert excinfo.value.failure_code == MCP_DISCOVERY_LIMIT_EXCEEDED


async def test_discover_tools_rejects_an_oversized_input_schema() -> None:
    fat = {"type": "object", "properties": {"q": {"type": "string", "description": "x" * 4096}}}

    with pytest.raises(McpRemoteError) as excinfo:
        await adapter_for(
            server_with([([tool("fat", schema=fat)], None)]),
            mcp_discovery_max_schema_bytes=1024,
        ).discover_tools(target(), None)

    assert excinfo.value.failure_code == MCP_TOOL_SCHEMA_TOO_LARGE


async def test_discover_tools_rejects_an_oversized_total_payload() -> None:
    # Each schema is comfortably under the per-tool budget; only their sum is
    # over the payload budget, which is the case this bound exists for.
    chunky = {"type": "object", "properties": {"q": {"type": "string", "description": "x" * 900}}}
    pages = [([tool(f"t{index}", schema=chunky) for index in range(10)], None)]

    with pytest.raises(McpRemoteError) as excinfo:
        await adapter_for(
            server_with(pages),
            mcp_discovery_max_schema_bytes=2048,
            mcp_discovery_max_payload_bytes=4096,
        ).discover_tools(target(), None)

    assert excinfo.value.failure_code == MCP_DISCOVERY_PAYLOAD_TOO_LARGE


async def test_the_payload_budget_covers_a_tool_beyond_its_schemas() -> None:
    """A server cannot stay small in its schemas and huge everywhere else.

    The schemas here are the ordinary tiny ones and pass the per-schema bound
    with room to spare; the weight is entirely in the description, which is
    just as much of the payload a caller has to receive.
    """

    windy = tool("windy", description="x" * 8192)

    with pytest.raises(McpRemoteError) as excinfo:
        await adapter_for(
            server_with([([windy], None)]),
            mcp_discovery_max_schema_bytes=2048,
            mcp_discovery_max_payload_bytes=4096,
        ).discover_tools(target(), None)

    assert excinfo.value.failure_code == MCP_DISCOVERY_PAYLOAD_TOO_LARGE


async def test_the_payload_budget_accumulates_across_pages() -> None:
    # No single tool is over the budget; the listing as a whole is, and paging
    # is not a way to deliver it in instalments.
    pages = [
        ([tool(f"a{index}", description="x" * 900) for index in range(3)], "1"),
        ([tool(f"b{index}", description="x" * 900) for index in range(3)], None),
    ]

    with pytest.raises(McpRemoteError) as excinfo:
        await adapter_for(
            server_with(pages),
            mcp_discovery_max_schema_bytes=2048,
            mcp_discovery_max_payload_bytes=4096,
        ).discover_tools(target(), None)

    assert excinfo.value.failure_code == MCP_DISCOVERY_PAYLOAD_TOO_LARGE


async def test_a_listing_within_the_payload_budget_still_comes_back_in_order() -> None:
    pages = [([tool("zeta"), tool("alpha")], "1"), ([tool("mid")], None)]

    outcome = await adapter_for(
        server_with(pages),
        mcp_discovery_max_schema_bytes=2048,
        mcp_discovery_max_payload_bytes=4096,
    ).discover_tools(target(), None)

    assert [entry.name for entry in outcome.tools] == ["zeta", "alpha", "mid"]


async def test_discover_tools_refuses_duplicate_tool_names_across_pages() -> None:
    pages = [([tool("search")], "1"), ([tool("search")], None)]

    with pytest.raises(McpRemoteError) as excinfo:
        await adapter_for(server_with(pages)).discover_tools(target(), None)

    assert excinfo.value.failure_code == MCP_DISCOVERY_INVALID


async def test_discover_tools_carries_annotations_without_interpreting_them() -> None:
    annotated = tool(
        "wipe",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )

    outcome = await adapter_for(server_with([([annotated], None)])).discover_tools(target(), None)

    entry = outcome.tools[0]
    assert entry.remote_annotations == {"read_only_hint": False, "destructive_hint": True}
    # 3A reports what the server said. Effect, risk and approval are governance
    # decisions made in 3B, so no such field may appear here.
    for forbidden in ("effect", "risk_level", "approval_policy", "execution_kind"):
        assert not hasattr(entry, forbidden)


async def test_discovery_failure_raises_rather_than_returning_an_empty_catalog() -> None:
    with pytest.raises(McpRemoteError) as excinfo:
        await failing_adapter(httpx2.ConnectError("boom")).discover_tools(target(), None)

    assert excinfo.value.failure_code == MCP_CONNECT_FAILED


async def test_default_client_refuses_a_private_endpoint_before_connecting() -> None:
    plain = McpClientAdapter(settings=settings())

    outcome = await plain.test_connection(target("http://127.0.0.1:9/mcp"), None)

    assert outcome.status == "unavailable"
    assert outcome.failure is not None
    assert outcome.failure.failure_code == MCP_TARGET_FORBIDDEN
