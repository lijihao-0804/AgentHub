"""The only place in AgentHub that speaks MCP.

Everything the MCP Python SDK exposes — the transport, the handshake, the
session, ``tools/list`` and its cursor — is confined to this module. Callers
receive normalized dataclasses and stable failure codes, so the service, the
API layer and the tests never import an SDK type and never have to reason
about protocol versions.

Three properties are enforced here rather than left to callers:

* the bearer token exists only for the duration of one outbound call, and
  appears in no long-lived object, log line or exception;
* every remote interaction is bounded in time, in tool count and in bytes;
* a remote failure is reported as a code from :data:`FAILURE_CODES`, never as
  an exception message, a URL, a response body or a remote stack trace.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter
from typing import Any, Literal
from uuid import UUID

import anyio
import httpx2
from jsonschema import Draft202012Validator, SchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import Implementation, Tool

from packages.core.config.settings import Settings
from packages.mcp.models import McpAuthType
from packages.mcp.security import (
    McpTargetError,
    authorize_endpoint,
    authorize_peer_address,
    parse_endpoint_url,
)

logger = logging.getLogger(__name__)

CLIENT_NAME = "agenthub"
CLIENT_VERSION = "0.1.0"

MCP_DNS_FAILED = "MCP_DNS_FAILED"
MCP_TARGET_FORBIDDEN = "MCP_TARGET_FORBIDDEN"
MCP_CONNECT_FAILED = "MCP_CONNECT_FAILED"
MCP_CONNECT_TIMEOUT = "MCP_CONNECT_TIMEOUT"
MCP_AUTH_FAILED = "MCP_AUTH_FAILED"
MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
MCP_SERVER_UNAVAILABLE = "MCP_SERVER_UNAVAILABLE"
MCP_DISCOVERY_LIMIT_EXCEEDED = "MCP_DISCOVERY_LIMIT_EXCEEDED"
MCP_DISCOVERY_PAYLOAD_TOO_LARGE = "MCP_DISCOVERY_PAYLOAD_TOO_LARGE"
MCP_TOOL_SCHEMA_TOO_LARGE = "MCP_TOOL_SCHEMA_TOO_LARGE"
MCP_DISCOVERY_INVALID = "MCP_DISCOVERY_INVALID"
MCP_TOOL_CALL_FAILED = "MCP_TOOL_CALL_FAILED"
MCP_TOOL_RESULT_TOO_LARGE = "MCP_TOOL_RESULT_TOO_LARGE"
MCP_TOOL_RESULT_UNSUPPORTED = "MCP_TOOL_RESULT_UNSUPPORTED"
MCP_TOOL_OUTPUT_INVALID = "MCP_TOOL_OUTPUT_INVALID"

FAILURE_CODES = frozenset(
    {
        MCP_DNS_FAILED,
        MCP_TARGET_FORBIDDEN,
        MCP_CONNECT_FAILED,
        MCP_CONNECT_TIMEOUT,
        MCP_AUTH_FAILED,
        MCP_PROTOCOL_ERROR,
        MCP_SERVER_UNAVAILABLE,
        MCP_DISCOVERY_LIMIT_EXCEEDED,
        MCP_DISCOVERY_PAYLOAD_TOO_LARGE,
        MCP_TOOL_SCHEMA_TOO_LARGE,
        MCP_DISCOVERY_INVALID,
        MCP_TOOL_CALL_FAILED,
        MCP_TOOL_RESULT_TOO_LARGE,
        MCP_TOOL_RESULT_UNSUPPORTED,
        MCP_TOOL_OUTPUT_INVALID,
    }
)

# Remote annotations are carried verbatim for a human to look at. They are
# hints from a server AgentHub does not control, so nothing downstream may turn
# them into an Effect, a risk level or an approval policy; that decision is
# governance's, made against the catalog, not against the server's own opinion
# of itself.
ANNOTATION_HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")


class McpRemoteError(Exception):
    """A remote interaction failed. Carries a stable code and nothing else."""

    def __init__(self, failure_code: str) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code


@dataclass(frozen=True)
class McpConnectionTarget:
    """The subset of a stored connection an outbound call needs."""

    connection_id: UUID
    endpoint_url: str
    auth_type: McpAuthType


@dataclass(frozen=True)
class NormalizedMcpServerInfo:
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None


@dataclass(frozen=True)
class NormalizedMcpTool:
    name: str
    title: str | None
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    remote_annotations: dict[str, bool] | None


@dataclass(frozen=True)
class NormalizedMcpFailure:
    failure_code: str


@dataclass(frozen=True)
class McpTestOutcome:
    status: Literal["healthy", "unavailable"]
    latency_ms: int
    server: NormalizedMcpServerInfo | None = None
    failure: NormalizedMcpFailure | None = None


@dataclass(frozen=True)
class McpDiscoveryOutcome:
    tools: tuple[NormalizedMcpTool, ...]
    server: NormalizedMcpServerInfo | None = None


class DispatchState(StrEnum):
    """How much is known about whether the remote was actually asked to act.

    This is the whole basis of the WRITE safety story. A request that provably
    never left this process can be failed outright, because nothing happened.
    Once the bytes have been handed to the transport, a silence afterwards is
    not evidence of anything: the server may well have done the work and lost
    the answer on the way back. That case is never reported as a failure.
    """

    NOT_DISPATCHED = "NOT_DISPATCHED"
    MAYBE_DISPATCHED = "MAYBE_DISPATCHED"
    DEFINITE_RESPONSE = "DEFINITE_RESPONSE"


class McpCallStatus(StrEnum):
    OK = "OK"
    TOOL_ERROR = "TOOL_ERROR"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class McpCallOutcome:
    """The only shape a tool call leaves this module in.

    ``TOOL_ERROR`` and ``FAILED`` are both definite: the first is the server
    saying the tool failed, the second is AgentHub knowing the call never went
    out. ``INDETERMINATE`` is the honest answer to everything else.
    """

    status: McpCallStatus
    dispatch: DispatchState
    data: dict[str, Any] | None = None
    failure_code: str | None = None


@dataclass
class _Observation:
    """What the transport saw, so the error mapper does not have to guess.

    The SDK turns a non-2xx response into a JSON-RPC error whose message no
    longer mentions the status, which is the right thing for the protocol but
    loses the one bit needed to tell "your token was rejected" from "the server
    is broken". The transport records that bit here; nothing else about the
    response is kept.
    """

    blocked_code: str | None = None
    last_http_status: int | None = None
    in_call_phase: bool = False
    call_dispatched: bool = False

    def mark_dispatched(self) -> None:
        """Record that a request is about to be handed to the network.

        Called from the guard immediately before the inner transport takes the
        request, which is the last moment at which "nothing has happened yet"
        is still true. Only dispatches during the ``tools/call`` round count:
        the handshake's own requests say nothing about whether the tool ran.
        """

        if self.in_call_phase:
            self.call_dispatched = True


ClientFactory = Callable[
    ["McpConnectionTarget", str | None, _Observation],
    AbstractAsyncContextManager[Client],
]


class GuardedAsyncTransport(httpx2.AsyncBaseTransport):
    """Re-applies the target policy to every request the SDK actually sends.

    The pre-flight check authorizes the configured endpoint. This guard covers
    what happens afterwards: any URL the transport derives from it must still
    be http/https, and the address the connection landed on must still be one
    AgentHub is allowed to reach. Redirects are handled above us — the SDK
    follows one only when it stays on the endpoint's origin — so this guard is
    about the socket, not about where a ``Location`` header points.
    """

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        allow_private_targets: bool,
        observation: _Observation,
    ) -> None:
        self._inner = inner
        self._allow_private_targets = allow_private_targets
        self._observation = observation

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            parse_endpoint_url(str(request.url))
        except McpTargetError:
            self._observation.blocked_code = MCP_TARGET_FORBIDDEN
            raise McpRemoteError(MCP_TARGET_FORBIDDEN) from None
        # Past this line nothing can be assumed not to have happened.
        self._observation.mark_dispatched()
        response = await self._inner.handle_async_request(request)
        self._observation.last_http_status = response.status_code
        try:
            authorize_peer_address(
                _peer_address(response), allow_private_targets=self._allow_private_targets
            )
        except McpTargetError as exc:
            self._observation.blocked_code = exc.failure_code
            await response.aclose()
            raise McpRemoteError(exc.failure_code) from None
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _peer_address(response: httpx2.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    try:
        info = stream.get_extra_info("server_addr")
    except Exception:  # pragma: no cover - transport-specific, never fatal
        return None
    if isinstance(info, tuple | list) and info:
        return str(info[0])
    if isinstance(info, str):
        return info
    return None


@dataclass(frozen=True)
class McpClientAdapter:
    """Speaks MCP on behalf of a workspace connection.

    Stateless by construction: a bearer token is passed in per call, lives in
    the per-call HTTP client, and goes away with it. ``client_factory`` exists
    so tests can drive a real SDK session against an in-process server without
    a socket; production always uses the guarded streamable-HTTP path.
    """

    settings: Settings
    client_factory: ClientFactory | None = field(default=None)

    async def test_connection(
        self, target: McpConnectionTarget, secret: str | None
    ) -> McpTestOutcome:
        """Connect, handshake, read the server's own description, disconnect.

        No tool is listed and none is executed. A remote that is unreachable or
        unhappy is a normal outcome of this call, not an error: it comes back
        as ``unavailable`` with a code.
        """

        started = perf_counter()
        observation = _Observation()
        try:
            with anyio.fail_after(self.settings.mcp_request_timeout_seconds):
                async with self._open(target, secret, observation) as client:
                    server = _server_info(client)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a code
            failure = _failure_code(exc, observation)
            latency_ms = _elapsed_ms(started)
            logger.info(
                "mcp.test connection_id=%s status=unavailable failure_code=%s duration_ms=%s",
                target.connection_id,
                failure,
                latency_ms,
            )
            return McpTestOutcome(
                status="unavailable",
                latency_ms=latency_ms,
                failure=NormalizedMcpFailure(failure_code=failure),
            )
        latency_ms = _elapsed_ms(started)
        logger.info(
            "mcp.test connection_id=%s status=healthy duration_ms=%s",
            target.connection_id,
            latency_ms,
        )
        return McpTestOutcome(status="healthy", latency_ms=latency_ms, server=server)

    async def discover_tools(
        self, target: McpConnectionTarget, secret: str | None
    ) -> McpDiscoveryOutcome:
        """Connect, handshake, page through ``tools/list``, normalize, disconnect.

        Nothing is called and nothing is written. Unlike :meth:`test_connection`,
        a failure here raises: an empty tool list is a meaningful answer from a
        healthy server, so it must not be how a broken one looks.
        """

        started = perf_counter()
        observation = _Observation()
        try:
            with anyio.fail_after(self.settings.mcp_request_timeout_seconds):
                async with self._open(target, secret, observation) as client:
                    server = _server_info(client)
                    tools = await self._collect_tools(client)
        except McpRemoteError as exc:
            _log_discovery_failure(target, exc.failure_code, started)
            raise
        except Exception as exc:  # noqa: BLE001 - every failure becomes a code
            failure = _failure_code(exc, observation)
            _log_discovery_failure(target, failure, started)
            raise McpRemoteError(failure) from None
        logger.info(
            "mcp.discover connection_id=%s status=ok tool_count=%s duration_ms=%s",
            target.connection_id,
            len(tools),
            _elapsed_ms(started),
        )
        return McpDiscoveryOutcome(tools=tools, server=server)

    async def call_tool(
        self,
        target: McpConnectionTarget,
        secret: str | None,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        timeout_seconds: float,
        max_result_bytes: int,
        output_schema: Mapping[str, Any] | None = None,
    ) -> McpCallOutcome:
        """Run one remote tool exactly once and normalize whatever comes back.

        The call is issued a single time. Nothing here retries — not on a
        timeout, not on a reset, not on a protocol error — because a retry of a
        request that may already have been carried out is how one approval
        becomes two side effects. The catalog is not re-read either: the tool
        name and schemas were frozen at publish and are used as frozen.
        """

        observation = _Observation()
        try:
            with anyio.fail_after(timeout_seconds):
                async with self._open(target, secret, observation) as client:
                    observation.in_call_phase = True
                    result = await client.call_tool(tool_name, dict(arguments))
                    observation.in_call_phase = False
                    return self._decide(
                        result, max_result_bytes=max_result_bytes, output_schema=output_schema
                    )
        except Exception as exc:  # noqa: BLE001 - every failure becomes a code
            failure = _failure_code(exc, observation)
            dispatch = (
                DispatchState.MAYBE_DISPATCHED
                if observation.call_dispatched
                else DispatchState.NOT_DISPATCHED
            )
            logger.info(
                "mcp.call connection_id=%s status=failed failure_code=%s dispatch=%s",
                target.connection_id,
                failure,
                dispatch.value,
            )
            if dispatch is DispatchState.MAYBE_DISPATCHED:
                return McpCallOutcome(
                    McpCallStatus.INDETERMINATE, dispatch, failure_code=failure
                )
            return McpCallOutcome(McpCallStatus.FAILED, dispatch, failure_code=failure)

    def _decide(
        self,
        result: Any,
        *,
        max_result_bytes: int,
        output_schema: Mapping[str, Any] | None,
    ) -> McpCallOutcome:
        """Turn a definite server response into an outcome.

        A response that arrived but cannot be handed on — too large, a payload
        shape this version does not carry, or structured output that breaks the
        contract frozen at import — is deliberately not reported as a failure.
        The server answered, which means it may well have acted, and saying
        "failed" about work that was done is the one lie this layer must not
        tell. It is indeterminate instead, and a human is asked.
        """

        try:
            data = _normalize_call_result(
                result, max_result_bytes=max_result_bytes, output_schema=output_schema
            )
        except McpRemoteError as exc:
            return McpCallOutcome(
                McpCallStatus.INDETERMINATE,
                DispatchState.DEFINITE_RESPONSE,
                failure_code=exc.failure_code,
            )
        if getattr(result, "is_error", False):
            # The server explicitly reported that the tool failed. That is an
            # answer, not an absence of one.
            return McpCallOutcome(
                McpCallStatus.TOOL_ERROR,
                DispatchState.DEFINITE_RESPONSE,
                data=data,
                failure_code=MCP_TOOL_CALL_FAILED,
            )
        return McpCallOutcome(McpCallStatus.OK, DispatchState.DEFINITE_RESPONSE, data=data)

    async def _collect_tools(self, client: Client) -> tuple[NormalizedMcpTool, ...]:
        max_tools = self.settings.mcp_discovery_max_tools
        max_payload = self.settings.mcp_discovery_max_payload_bytes
        max_schema = self.settings.mcp_discovery_max_schema_bytes

        normalized: list[NormalizedMcpTool] = []
        seen: set[str] = set()
        payload_bytes = 0
        cursor: str | None = None
        # A server controls its own cursors, so a cursor loop is a server-
        # controlled loop. The tool budget is what terminates it; there is no
        # path here that pages forever.
        while True:
            result = await client.list_tools(cursor=cursor)
            for tool in result.tools:
                if len(normalized) >= max_tools:
                    raise McpRemoteError(MCP_DISCOVERY_LIMIT_EXCEEDED)
                entry, size = _normalize_tool(tool, max_schema_bytes=max_schema)
                if entry.name in seen:
                    # Two tools under one name cannot both be addressed later.
                    # Refusing the whole listing is the only answer that does
                    # not silently pick a winner on the operator's behalf.
                    raise McpRemoteError(MCP_DISCOVERY_INVALID)
                payload_bytes += size
                if payload_bytes > max_payload:
                    raise McpRemoteError(MCP_DISCOVERY_PAYLOAD_TOO_LARGE)
                seen.add(entry.name)
                normalized.append(entry)
            cursor = result.next_cursor
            if cursor is None:
                break
        # Server order is preserved across pages. Discovery is a view of what
        # the server declared, in the order it declared it.
        return tuple(normalized)

    def _open(
        self, target: McpConnectionTarget, secret: str | None, observation: _Observation
    ) -> AbstractAsyncContextManager[Client]:
        factory = self.client_factory or self._default_client
        return factory(target, secret, observation)

    @asynccontextmanager
    async def _default_client(
        self, target: McpConnectionTarget, secret: str | None, observation: _Observation
    ) -> AsyncIterator[Client]:
        allow_private = self.settings.mcp_allow_private_targets
        endpoint = parse_endpoint_url(target.endpoint_url)
        try:
            await authorize_endpoint(endpoint, allow_private_targets=allow_private)
        except McpTargetError as exc:
            raise McpRemoteError(exc.failure_code) from None

        headers: dict[str, str] = {}
        if target.auth_type is McpAuthType.BEARER and secret:
            # The one place the token is used. It is written into a client that
            # lives for this call only, and the adapter itself never holds it.
            headers["Authorization"] = f"Bearer {secret}"

        timeout = httpx2.Timeout(
            connect=self.settings.mcp_connect_timeout_seconds,
            read=self.settings.mcp_request_timeout_seconds,
            write=self.settings.mcp_request_timeout_seconds,
            pool=self.settings.mcp_connect_timeout_seconds,
        )
        transport = GuardedAsyncTransport(
            httpx2.AsyncHTTPTransport(),
            allow_private_targets=allow_private,
            observation=observation,
        )
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=timeout,
            transport=transport,
            # The SDK follows a redirect only within the endpoint's origin and
            # does not consult this flag; it is set anyway so that anything
            # else reaching for this client inherits the same answer.
            follow_redirects=False,
        ) as http_client:
            async with Client(
                streamable_http_client(endpoint.url, http_client=http_client),
                read_timeout_seconds=self.settings.mcp_request_timeout_seconds,
                client_info=Implementation(name=CLIENT_NAME, version=CLIENT_VERSION),
                # Discovery must reflect the server as it is now, and a cache
                # keyed by endpoint would outlive the credential that read it.
                cache=None,
            ) as client:
                yield client


def _normalize_call_result(
    result: Any,
    *,
    max_result_bytes: int,
    output_schema: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reduce a ``CallToolResult`` to plain JSON an agent may safely be shown.

    No SDK object survives this function. Text content is carried through as
    text and structured content as ordinary JSON; anything else — an image, an
    audio clip, an embedded resource — is refused rather than smuggled into a
    model's context as a wall of base64 nobody asked for.
    """

    texts: list[str] = []
    for block in getattr(result, "content", None) or ():
        if getattr(block, "type", None) != "text" or not isinstance(
            getattr(block, "text", None), str
        ):
            raise McpRemoteError(MCP_TOOL_RESULT_UNSUPPORTED)
        texts.append(block.text)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        structured = _plain_json(structured)
        if output_schema is not None:
            # The schema frozen at import is the contract. A server that has
            # since changed its mind does not get to redefine it mid-run.
            try:
                Draft202012Validator(dict(output_schema)).validate(structured)
            except (JsonSchemaValidationError, SchemaError):
                raise McpRemoteError(MCP_TOOL_OUTPUT_INVALID) from None
    payload = {"content": texts, "structured_content": structured}
    if _json_size(payload) > max_result_bytes:
        # Not truncated: half a result presented as a whole one is worse than
        # no result, because nothing downstream can tell the difference.
        raise McpRemoteError(MCP_TOOL_RESULT_TOO_LARGE)
    return payload


def _plain_json(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain_json(item) for item in value]
    raise McpRemoteError(MCP_TOOL_RESULT_UNSUPPORTED)


def _normalize_tool(tool: Tool, *, max_schema_bytes: int) -> tuple[NormalizedMcpTool, int]:
    schema = tool.input_schema
    if not isinstance(schema, dict):
        raise McpRemoteError(MCP_DISCOVERY_INVALID)
    if _json_size(schema) > max_schema_bytes:
        raise McpRemoteError(MCP_TOOL_SCHEMA_TOO_LARGE)
    output_schema = tool.output_schema if isinstance(tool.output_schema, dict) else None
    if output_schema is not None and _json_size(output_schema) > max_schema_bytes:
        raise McpRemoteError(MCP_TOOL_SCHEMA_TOO_LARGE)
    entry = NormalizedMcpTool(
        name=tool.name,
        title=tool.title,
        description=tool.description,
        input_schema=schema,
        output_schema=output_schema,
        remote_annotations=_remote_annotations(tool),
    )
    return entry, _payload_size(entry)


def _payload_size(entry: NormalizedMcpTool) -> int:
    """Measure everything discovery would hand back for one tool.

    The per-schema bound is deliberately not what limits the listing: a server
    can stay under it on every schema and still answer with megabytes of
    descriptions or titles. What is counted here is the whole normalized
    entry — name, title, description, both schemas and the remote annotations —
    serialized exactly once, compactly and with sorted keys, so the number is
    the same for the same tool on every run.
    """

    return _json_size(
        {
            "name": entry.name,
            "title": entry.title,
            "description": entry.description,
            "input_schema": entry.input_schema,
            "output_schema": entry.output_schema,
            "remote_annotations": entry.remote_annotations,
        }
    )


def _remote_annotations(tool: Tool) -> dict[str, bool] | None:
    annotations = tool.annotations
    if annotations is None:
        return None
    hints = {
        name: value
        for name in ANNOTATION_HINTS
        if isinstance(value := getattr(annotations, name, None), bool)
    }
    return hints or None


def _json_size(value: Any) -> int:
    try:
        serialized = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, sort_keys=True
        )
    except (TypeError, ValueError):
        raise McpRemoteError(MCP_DISCOVERY_INVALID) from None
    return len(serialized.encode("utf-8"))


def _server_info(client: Client) -> NormalizedMcpServerInfo:
    try:
        protocol_version = client.protocol_version
    except Exception:  # noqa: BLE001 - metadata is best effort, never fatal
        protocol_version = None
    info = client.server_info
    return NormalizedMcpServerInfo(
        protocol_version=protocol_version,
        server_name=info.name if info is not None else None,
        server_version=info.version if info is not None else None,
    )


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _log_discovery_failure(target: McpConnectionTarget, failure_code: str, started: float) -> None:
    logger.info(
        "mcp.discover connection_id=%s status=failed failure_code=%s duration_ms=%s",
        target.connection_id,
        failure_code,
        _elapsed_ms(started),
    )


def _iter_causes(exc: BaseException) -> Iterator[BaseException]:
    """Flatten an exception into everything it is standing in for.

    anyio runs the transport in a task group, so a connect failure arrives
    wrapped in an ExceptionGroup with the real cause several links down. The
    mapper has to see all of it to classify honestly.
    """

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if current.__context__ is not None:
            stack.append(current.__context__)


def _failure_code(exc: BaseException, observation: _Observation) -> str:
    """Classify a failure into one stable code, revealing nothing about it."""

    if observation.blocked_code is not None:
        return observation.blocked_code
    causes = list(_iter_causes(exc))
    for cause in causes:
        if isinstance(cause, McpRemoteError):
            return cause.failure_code
        if isinstance(cause, McpTargetError):
            return cause.failure_code
    if observation.last_http_status in (401, 403):
        return MCP_AUTH_FAILED
    for cause in causes:
        if isinstance(cause, httpx2.ConnectTimeout | httpx2.PoolTimeout):
            return MCP_CONNECT_TIMEOUT
    for cause in causes:
        if isinstance(cause, httpx2.ConnectError | httpx2.ProxyError):
            return MCP_CONNECT_FAILED
    for cause in causes:
        if isinstance(cause, MCPError | httpx2.ProtocolError):
            return MCP_PROTOCOL_ERROR
    for cause in causes:
        if isinstance(cause, httpx2.TimeoutException | TimeoutError):
            return MCP_SERVER_UNAVAILABLE
    return MCP_SERVER_UNAVAILABLE


__all__ = [
    "FAILURE_CODES",
    "MCP_AUTH_FAILED",
    "MCP_CONNECT_FAILED",
    "MCP_CONNECT_TIMEOUT",
    "MCP_DISCOVERY_INVALID",
    "MCP_DISCOVERY_LIMIT_EXCEEDED",
    "MCP_DISCOVERY_PAYLOAD_TOO_LARGE",
    "MCP_DNS_FAILED",
    "MCP_PROTOCOL_ERROR",
    "MCP_SERVER_UNAVAILABLE",
    "MCP_TARGET_FORBIDDEN",
    "MCP_TOOL_CALL_FAILED",
    "MCP_TOOL_OUTPUT_INVALID",
    "MCP_TOOL_RESULT_TOO_LARGE",
    "MCP_TOOL_RESULT_UNSUPPORTED",
    "MCP_TOOL_SCHEMA_TOO_LARGE",
    "DispatchState",
    "GuardedAsyncTransport",
    "McpCallOutcome",
    "McpCallStatus",
    "McpClientAdapter",
    "McpConnectionTarget",
    "McpDiscoveryOutcome",
    "McpRemoteError",
    "McpTestOutcome",
    "NormalizedMcpFailure",
    "NormalizedMcpServerInfo",
    "NormalizedMcpTool",
]
