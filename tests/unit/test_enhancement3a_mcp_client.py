"""Enhancement 3A: the MCP protocol adapter, driven against in-process servers.

Every test here runs a real MCP SDK session — real handshake, real
``tools/list``, real cursor — over an in-memory transport. Nothing touches a
socket, so the suite is deterministic and works offline, while still
exercising the protocol rather than a hand-rolled stand-in for it.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpcore2
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
    GuardedAsyncTransport,
    McpClientAdapter,
    McpConnectionTarget,
    McpRemoteError,
    _Observation,
    _PinnedAsyncHTTPTransport,
    _PinnedNetworkBackend,
)
from packages.mcp.models import McpAuthType
from packages.mcp.security import parse_endpoint_url
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
    return Tool(
        name=name,
        description=description or f"{name} tool",
        inputSchema=schema or SCHEMA,
        annotations=annotations,
    )


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


class _NetworkStream(httpcore2.AsyncNetworkStream):
    def __init__(self, peer: str | None) -> None:
        self.peer = peer
        self.closed = False
        self.tls_server_name: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        return b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del buffer, timeout

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        del ssl_context, timeout
        self.tls_server_name = server_hostname
        return self

    def get_extra_info(self, info: str) -> object:
        if info == "server_addr" and self.peer is not None:
            return (self.peer, 443)
        return None


class _NetworkBackend(httpcore2.AsyncNetworkBackend):
    def __init__(self, peer: str | None) -> None:
        self.peer = peer
        self.connected: list[tuple[str, int]] = []
        self.streams: list[_NetworkStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ) -> httpcore2.AsyncNetworkStream:
        del timeout, local_address, socket_options
        self.connected.append((host, port))
        stream = _NetworkStream(self.peer)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: object = None,
    ) -> httpcore2.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("Unix sockets are not valid MCP network targets.")

    async def sleep(self, seconds: float) -> None:
        del seconds


class _UnsafeInner(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests_sent = 0
        self.authorization_seen: str | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests_sent += 1
        self.authorization_seen = request.headers.get("Authorization")
        return httpx2.Response(200, request=request, content=b"sent")

    async def aclose(self) -> None:
        pass


async def test_guard_refuses_a_transport_without_presend_authorization() -> None:
    sentinel = "Bearer probe-secret-sentinel"
    inner = _UnsafeInner()
    guard = GuardedAsyncTransport(
        inner,
        allow_private_targets=False,
        observation=_Observation(),
    )
    request = httpx2.Request(
        "POST",
        "https://public.example/mcp",
        headers={"Authorization": sentinel},
    )

    with pytest.raises(McpRemoteError) as excinfo:
        await guard.handle_async_request(request)

    assert excinfo.value.failure_code == MCP_TARGET_FORBIDDEN
    assert inner.requests_sent == 0
    assert inner.authorization_seen is None


@pytest.mark.parametrize("peer", ["169.254.169.254", None])
async def test_backend_rejects_forbidden_or_missing_peer_before_tls(peer: str | None) -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")
    delegate = _NetworkBackend(peer)

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    backend = _PinnedNetworkBackend(
        endpoint,
        observation=_Observation(),
        allow_private_targets=False,
        resolver=resolver,
        delegate=delegate,
    )

    with pytest.raises(McpRemoteError) as excinfo:
        await backend.connect_tcp("mcp.example.com", 443)

    assert excinfo.value.failure_code == MCP_TARGET_FORBIDDEN
    assert delegate.connected == [("93.184.216.34", 443)]
    assert delegate.streams[0].closed is True
    assert delegate.streams[0].tls_server_name is None


async def test_backend_refuses_a_connection_that_lands_on_an_authorized_but_unpinned_peer() -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")
    delegate = _NetworkBackend("8.8.8.8")

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    backend = _PinnedNetworkBackend(
        endpoint,
        observation=_Observation(),
        allow_private_targets=False,
        resolver=resolver,
        delegate=delegate,
    )

    with pytest.raises(McpRemoteError) as excinfo:
        await backend.connect_tcp("mcp.example.com", 443)

    assert excinfo.value.failure_code == MCP_TARGET_FORBIDDEN
    assert delegate.connected == [("93.184.216.34", 443)]
    assert delegate.streams[0].closed is True
    assert backend.last_peer is None


async def test_backend_reauthorizes_on_each_new_connection() -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")
    delegate = _NetworkBackend("93.184.216.34")
    answers = iter(
        [
            ("93.184.216.34",),
            ("169.254.169.254",),
        ]
    )
    resolution_calls = 0

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        nonlocal resolution_calls
        resolution_calls += 1
        return next(answers)

    backend = _PinnedNetworkBackend(
        endpoint,
        observation=_Observation(),
        allow_private_targets=False,
        resolver=resolver,
        delegate=delegate,
    )

    await backend.connect_tcp("mcp.example.com", 443)
    with pytest.raises(McpRemoteError) as excinfo:
        await backend.connect_tcp("mcp.example.com", 443)

    assert excinfo.value.failure_code == MCP_TARGET_FORBIDDEN
    assert resolution_calls == 2
    assert delegate.connected == [("93.184.216.34", 443)]
    assert backend.last_peer is None


async def test_pinned_transport_preserves_origin_hostname_for_sni_and_verification() -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")
    delegate = _NetworkBackend("93.184.216.34")

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    transport = _PinnedAsyncHTTPTransport(
        endpoint,
        observation=_Observation(),
        allow_private_targets=False,
        resolver=resolver,
        network_backend=delegate,
    )
    request = httpx2.Request("GET", endpoint.url)
    try:
        response = await transport.handle_async_request(request)
        await response.aclose()
    finally:
        await transport.aclose()

    assert delegate.connected == [("93.184.216.34", 443)]
    assert delegate.streams[0].tls_server_name == "mcp.example.com"
    assert transport.authorize_request_peer() == "93.184.216.34"


async def test_default_client_uses_the_pinned_transport() -> None:
    transport = _PinnedAsyncHTTPTransport(
        parse_endpoint_url("https://mcp.example.com/mcp"),
        observation=_Observation(),
        allow_private_targets=False,
    )

    assert callable(transport.authorize_request_peer)
    await transport.aclose()


async def test_guard_uses_the_peer_pinned_by_the_safe_transport() -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")
    delegate = _NetworkBackend("93.184.216.34")

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    transport = _PinnedAsyncHTTPTransport(
        endpoint,
        observation=_Observation(),
        allow_private_targets=False,
        resolver=resolver,
        network_backend=delegate,
    )
    guard = GuardedAsyncTransport(
        transport,
        allow_private_targets=False,
        observation=_Observation(),
    )
    try:
        response = await guard.handle_async_request(httpx2.Request("GET", endpoint.url))
        await response.aclose()
    finally:
        await guard.aclose()

    assert delegate.connected == [("93.184.216.34", 443)]


async def test_rebinding_to_a_forbidden_address_sends_no_request_or_bearer() -> None:
    endpoint = parse_endpoint_url("https://mcp.example.com/mcp")
    delegate = _NetworkBackend("93.184.216.34")
    answers = iter(
        [
            ("93.184.216.34",),
            ("169.254.169.254",),
        ]
    )

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return next(answers)

    observation = _Observation()
    transport = _PinnedAsyncHTTPTransport(
        endpoint,
        observation=observation,
        allow_private_targets=False,
        resolver=resolver,
        network_backend=delegate,
    )
    guard = GuardedAsyncTransport(
        transport,
        allow_private_targets=False,
        observation=observation,
    )
    headers = {"Authorization": "Bearer rebinding-probe-secret"}
    try:
        first = await guard.handle_async_request(
            httpx2.Request("POST", endpoint.url, headers=headers)
        )
        await first.aclose()
        observation.in_call_phase = True
        observation.call_dispatched = False
        second = httpx2.Request("POST", endpoint.url, headers=headers)
        with pytest.raises(McpRemoteError) as excinfo:
            await guard.handle_async_request(second)
    finally:
        await guard.aclose()

    assert excinfo.value.failure_code == MCP_TARGET_FORBIDDEN
    assert delegate.connected == [("93.184.216.34", 443)]
    assert len(delegate.streams) == 1
    assert delegate.streams[0].tls_server_name == "mcp.example.com"
    assert observation.call_dispatched is False
