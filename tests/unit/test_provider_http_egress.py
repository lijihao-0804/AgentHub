from __future__ import annotations

import json
import socket
import ssl
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpcore
import httpx
import pytest
import pytest_asyncio
from httpcore._backends.mock import AsyncMockStream

from packages.model_gateway.adapters.litellm.adapter import LiteLLMProviderAdapter
from packages.model_gateway.adapters.litellm.provider_http import (
    GuardedProviderTransport,
    _PinnedProviderBackend,
)
from packages.model_gateway.contracts import ModelMessage, ModelRequest, ModelStreamEventType
from packages.model_gateway.egress import (
    PEER_FORBIDDEN,
    TARGET_FORBIDDEN,
    ProviderEgressError,
    ProviderEgressPolicy,
    ProviderEndpoint,
    parse_provider_endpoint,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile, ProviderCredential

_SYNTHETIC_SECRET = "synthetic-provider-key"


@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch: pytest.MonkeyPatch):
    attempts: list[str] = []

    # LiteLLM otherwise fetches its metadata during import. This switch is
    # documented by the installed SDK and keeps import-time setup off-network.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")

    async def reject_async(kind: str, *args, **kwargs) -> None:
        del args, kwargs
        attempts.append(kind)
        raise AssertionError(f"real network path attempted: {kind}")

    def reject_sync(kind: str, *args, **kwargs) -> None:
        del args, kwargs
        attempts.append(kind)
        raise AssertionError(f"real network path attempted: {kind}")

    async def reject_async_tcp(*args, **kwargs) -> None:
        await reject_async("httpcore.AnyIOBackend.connect_tcp", *args, **kwargs)

    async def reject_async_unix(*args, **kwargs) -> None:
        await reject_async("httpcore.AnyIOBackend.connect_unix_socket", *args, **kwargs)

    def reject_sync_tcp(*args, **kwargs) -> None:
        reject_sync("httpcore.SyncBackend.connect_tcp", *args, **kwargs)

    def reject_sync_unix(*args, **kwargs) -> None:
        reject_sync("httpcore.SyncBackend.connect_unix_socket", *args, **kwargs)

    def reject_socket_connection(*args, **kwargs) -> None:
        reject_sync("socket.create_connection", *args, **kwargs)

    def reject_dns(*args, **kwargs) -> None:
        reject_sync("socket.getaddrinfo", *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", reject_async_tcp)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_unix_socket", reject_async_unix)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", reject_sync_tcp)
    monkeypatch.setattr(httpcore.SyncBackend, "connect_unix_socket", reject_sync_unix)
    monkeypatch.setattr(socket, "create_connection", reject_socket_connection)
    monkeypatch.setattr(socket, "getaddrinfo", reject_dns)
    yield
    assert attempts == [], "tests attempted real network I/O: " + ", ".join(attempts)


@pytest_asyncio.fixture(autouse=True)
async def _stop_litellm_logging_worker():
    yield
    if "litellm" not in sys.modules:
        return
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    await GLOBAL_LOGGING_WORKER.stop()


class _Resolver:
    def __init__(self, *answers: Iterable[str]) -> None:
        self.answers = tuple(tuple(answer) for answer in answers)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        index = len(self.calls) - 1
        if index >= len(self.answers):
            raise AssertionError("unexpected DNS resolution")
        return self.answers[index]


class _RecordingStream(AsyncMockStream):
    def __init__(self, response: bytes, peer_address: str) -> None:
        super().__init__([response])
        self.peer_address = peer_address
        self.writes: list[bytes] = []
        self.tls_calls: list[tuple[ssl.SSLContext, str | None]] = []

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del timeout
        self.writes.append(bytes(buffer))

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.tls_calls.append((ssl_context, server_hostname))
        await super().start_tls(ssl_context, server_hostname=server_hostname, timeout=timeout)
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "server_addr":
            return (self.peer_address, 443)
        return super().get_extra_info(info)


class _FakeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, response: bytes, *, peer_address: str = "8.8.8.8") -> None:
        self.response = response
        self.peer_address = peer_address
        self.connect_calls: list[tuple[str, int]] = []
        self.unix_calls: list[str] = []
        self.streams: list[_RecordingStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout, local_address, socket_options
        self.connect_calls.append((host, port))
        stream = _RecordingStream(self.response, self.peer_address)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[int, int, int]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout, socket_options
        self.unix_calls.append(path)
        raise AssertionError("production network backend must not be reached")

    async def sleep(self, seconds: float) -> None:
        del seconds


@dataclass(slots=True)
class _BuiltTransport:
    endpoint: ProviderEndpoint
    policy: ProviderEgressPolicy
    backend: _FakeBackend
    transport: GuardedProviderTransport


class _RecordingFactory:
    def __init__(self, response: bytes, *, peer_address: str = "8.8.8.8") -> None:
        self.response = response
        self.peer_address = peer_address
        self.built: list[_BuiltTransport] = []

    def __call__(
        self,
        endpoint: ProviderEndpoint,
        policy: ProviderEgressPolicy,
    ) -> GuardedProviderTransport:
        backend = _FakeBackend(self.response, peer_address=self.peer_address)
        transport = GuardedProviderTransport(endpoint, policy, network_backend=backend)
        self.built.append(_BuiltTransport(endpoint, policy, backend, transport))
        return transport


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/json",
    extra_headers: Iterable[tuple[str, str]] = (),
) -> bytes:
    reason = "OK" if status == 200 else "FOUND"
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in extra_headers),
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + body


def _completion_body(model: str, content: str = "guarded") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
        separators=(",", ":"),
    ).encode()


def _stream_body(model: str) -> bytes:
    chunks = [
        {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": "streamed"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
    ]
    return (
        "".join(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def _profile(
    provider: str,
    *,
    base_url: str | None,
    model: str,
) -> tuple[ModelProfile, ProviderCredential]:
    workspace_id = uuid4()
    credential = ProviderCredential(
        workspace_id=workspace_id,
        provider=provider,
        name="synthetic provider",
        secret=_SYNTHETIC_SECRET,
        base_url=base_url,
    )
    profile = ModelProfile(
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model=model,
        temperature=Decimal("0"),
        max_tokens=32,
        timeout_seconds=Decimal("5"),
    )
    return profile, credential


async def _post(
    transport: httpx.AsyncBaseTransport,
    url: str,
    *,
    follow_redirects: bool = False,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=follow_redirects,
    ) as client:
        return await client.post(url, content=b"synthetic-request")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        ("10.0.0.9",),
        ("8.8.8.8", "10.0.0.9"),
    ],
)
async def test_private_or_mixed_dns_is_rejected_before_connect(answers) -> None:
    resolver = _Resolver(answers)
    policy = ProviderEgressPolicy(resolver=resolver)
    backend = _FakeBackend(_http_response(b"{}"))
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    transport = GuardedProviderTransport(endpoint, policy, network_backend=backend)

    with pytest.raises(ProviderEgressError) as raised:
        await _post(transport, "https://provider.example/v1/chat/completions")

    assert raised.value.failure_code == TARGET_FORBIDDEN
    assert transport.blocked_failure_code == TARGET_FORBIDDEN
    assert resolver.calls == [("provider.example", 443)]
    assert backend.connect_calls == []
    assert backend.streams == []


@pytest.mark.asyncio
async def test_authorized_public_address_is_pinned_and_tls_keeps_hostname() -> None:
    resolver = _Resolver(("8.8.8.8",))
    backend = _FakeBackend(_http_response(b"{}"))
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    transport = GuardedProviderTransport(
        endpoint,
        ProviderEgressPolicy(resolver=resolver),
        network_backend=backend,
    )

    response = await _post(transport, "https://provider.example/v1/chat/completions")

    assert response.status_code == 200
    assert backend.connect_calls == [("8.8.8.8", 443)]
    stream = backend.streams[0]
    assert len(stream.tls_calls) == 1
    context, server_hostname = stream.tls_calls[0]
    assert server_hostname == "provider.example"
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    request_bytes = b"".join(stream.writes)
    assert request_bytes.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
    assert b"host: provider.example\r\n" in request_bytes.lower()


@pytest.mark.asyncio
async def test_peer_mismatch_closes_socket_before_request_bytes() -> None:
    backend = _FakeBackend(_http_response(b"{}"), peer_address="8.8.4.4")
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    transport = GuardedProviderTransport(
        endpoint,
        ProviderEgressPolicy(resolver=_Resolver(("8.8.8.8",))),
        network_backend=backend,
    )

    with pytest.raises(ProviderEgressError) as raised:
        await _post(transport, "https://provider.example/v1/chat/completions")

    assert raised.value.failure_code == PEER_FORBIDDEN
    assert backend.connect_calls == [("8.8.8.8", 443)]
    assert backend.streams[0].writes == []
    assert backend.streams[0]._closed is True


@pytest.mark.asyncio
async def test_each_new_connection_rechecks_dns_and_rejects_rebinding() -> None:
    resolver = _Resolver(("8.8.8.8",), ("10.0.0.9",))
    backend = _FakeBackend(_http_response(b"{}"))
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    transport = GuardedProviderTransport(
        endpoint,
        ProviderEgressPolicy(resolver=resolver),
        network_backend=backend,
    )

    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        first = await client.post("https://provider.example/v1/chat/completions", content=b"first")
        assert first.status_code == 200
        with pytest.raises(ProviderEgressError) as raised:
            await client.post("https://provider.example/v1/chat/completions", content=b"second")

    assert raised.value.failure_code == TARGET_FORBIDDEN
    assert resolver.calls == [("provider.example", 443), ("provider.example", 443)]
    assert backend.connect_calls == [("8.8.8.8", 443)]
    assert len(backend.streams) == 1
    assert backend.streams[0].writes


@pytest.mark.asyncio
async def test_redirect_is_refused_even_when_upper_client_follows_redirects() -> None:
    resolver = _Resolver(("8.8.8.8",))
    backend = _FakeBackend(
        _http_response(
            b"",
            status=302,
            extra_headers=(("Location", "https://collector.example/private"),),
        )
    )
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    transport = GuardedProviderTransport(
        endpoint,
        ProviderEgressPolicy(resolver=resolver),
        network_backend=backend,
    )

    with pytest.raises(ProviderEgressError) as raised:
        await _post(
            transport,
            "https://provider.example/v1/chat/completions",
            follow_redirects=True,
        )

    assert raised.value.failure_code == TARGET_FORBIDDEN
    assert resolver.calls == [("provider.example", 443)]
    assert backend.connect_calls == [("8.8.8.8", 443)]
    assert all(stream.writes for stream in backend.streams)


@pytest.mark.asyncio
async def test_cross_origin_request_and_unix_socket_are_refused() -> None:
    resolver = _Resolver()
    backend = _FakeBackend(_http_response(b"{}"))
    endpoint = parse_provider_endpoint("https://provider.example/v1")
    policy = ProviderEgressPolicy(resolver=resolver)
    transport = GuardedProviderTransport(endpoint, policy, network_backend=backend)

    with pytest.raises(ProviderEgressError) as cross_origin:
        await _post(transport, "https://other.example/v1/chat/completions")
    pinned_backend = _PinnedProviderBackend(endpoint, policy, backend)
    with pytest.raises(ProviderEgressError) as unix_socket:
        await pinned_backend.connect_unix_socket("/tmp/provider.sock")

    assert cross_origin.value.failure_code == TARGET_FORBIDDEN
    assert unix_socket.value.failure_code == TARGET_FORBIDDEN
    assert resolver.calls == []
    assert backend.connect_calls == []
    assert backend.unix_calls == []


@pytest.mark.asyncio
async def test_explicit_loopback_exception_is_exact_cidr_scoped() -> None:
    allowed_backend = _FakeBackend(_http_response(b"{}"), peer_address="127.0.0.1")
    allowed_endpoint = parse_provider_endpoint("http://127.0.0.1:8080")
    allowed_transport = GuardedProviderTransport(
        allowed_endpoint,
        ProviderEgressPolicy(
            private_cidrs=("127.0.0.1/32",),
            resolver=_Resolver(("127.0.0.1",)),
        ),
        network_backend=allowed_backend,
    )

    response = await _post(allowed_transport, "http://127.0.0.1:8080/v1/chat/completions")

    assert response.status_code == 200
    assert allowed_backend.connect_calls == [("127.0.0.1", 8080)]

    denied_resolver = _Resolver(("127.0.0.2",))
    denied_backend = _FakeBackend(_http_response(b"{}"), peer_address="127.0.0.2")
    denied_endpoint = parse_provider_endpoint("http://127.0.0.2:8080")
    denied_transport = GuardedProviderTransport(
        denied_endpoint,
        ProviderEgressPolicy(
            private_cidrs=("127.0.0.1/32",),
            resolver=denied_resolver,
        ),
        network_backend=denied_backend,
    )
    with pytest.raises(ProviderEgressError) as raised:
        await _post(denied_transport, "http://127.0.0.2:8080/v1/chat/completions")

    assert raised.value.failure_code == TARGET_FORBIDDEN
    assert denied_backend.connect_calls == []
    assert denied_backend.streams == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-synthetic"),
        ("openai-compatible", "compatible-synthetic"),
    ],
)
async def test_default_litellm_openai_sdk_uses_guarded_v1_default(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment-route.example/v1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    resolver = _Resolver(("8.8.8.8",))
    factory = _RecordingFactory(_http_response(_completion_body(model)))
    adapter = LiteLLMProviderAdapter(
        egress_policy=ProviderEgressPolicy(resolver=resolver),
        egress_transport_factory=factory,
    )
    profile, credential = _profile(provider, base_url=None, model=model)

    response = await adapter.complete(
        profile,
        credential,
        ModelRequest(messages=(ModelMessage(role="user", content="synthetic"),)),
    )

    assert response.content == "guarded"
    assert resolver.calls == [("api.openai.com", 443)]
    assert len(factory.built) == 1
    assert factory.built[0].endpoint.url == "https://api.openai.com/v1"
    assert factory.built[0].backend.connect_calls == [("8.8.8.8", 443)]
    assert factory.built[0].backend.streams[0].tls_calls[0][1] == "api.openai.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [("openai-compatible", "compatible-synthetic"), ("deepseek", "deepseek-chat")],
    ids=["openai-compatible", "deepseek"],
)
async def test_default_litellm_sdk_stream_uses_guard_through_completion(provider, model) -> None:
    factory = _RecordingFactory(
        _http_response(_stream_body(model), content_type="text/event-stream")
    )
    adapter = LiteLLMProviderAdapter(
        egress_policy=ProviderEgressPolicy(resolver=_Resolver(("8.8.8.8",))),
        egress_transport_factory=factory,
    )
    profile, credential = _profile(
        provider,
        base_url="https://compatible.example/v1",
        model=model,
    )

    events = [
        event
        async for event in adapter.stream(
            profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="synthetic"),)),
        )
    ]

    deltas = [
        event.message_delta
        for event in events
        if event.event_type is ModelStreamEventType.MESSAGE_DELTA
    ]
    completed = [
        event.response for event in events if event.event_type is ModelStreamEventType.COMPLETED
    ]
    assert deltas == ["streamed"]
    assert completed and completed[0].content == "streamed"
    assert factory.built[0].endpoint.host == "compatible.example"
    assert factory.built[0].backend.streams[0].tls_calls[0][1] == "compatible.example"


@pytest.mark.asyncio
async def test_default_litellm_deepseek_health_uses_guarded_async_handler() -> None:
    model = "deepseek-chat"
    factory = _RecordingFactory(_http_response(_completion_body(model, "healthy")))
    resolver = _Resolver(("8.8.8.8",))
    adapter = LiteLLMProviderAdapter(
        egress_policy=ProviderEgressPolicy(resolver=resolver),
        egress_transport_factory=factory,
    )
    profile, credential = _profile("deepseek", base_url=None, model=model)

    await adapter.health(profile, credential)

    assert resolver.calls == [("api.deepseek.com", 443)]
    assert factory.built[0].endpoint.url == "https://api.deepseek.com"
    assert factory.built[0].backend.connect_calls == [("8.8.8.8", 443)]
    assert factory.built[0].backend.streams[0].tls_calls[0][1] == "api.deepseek.com"


@pytest.mark.asyncio
async def test_sdk_wrapped_egress_refusal_maps_to_safe_nonretryable_error() -> None:
    factory = _RecordingFactory(_http_response(_completion_body("gpt-synthetic")))
    adapter = LiteLLMProviderAdapter(
        egress_policy=ProviderEgressPolicy(resolver=_Resolver(("10.0.0.9",))),
        egress_transport_factory=factory,
    )
    profile, credential = _profile("openai", base_url=None, model="gpt-synthetic")

    with pytest.raises(ModelGatewayError) as raised:
        await adapter.complete(
            profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="synthetic"),)),
        )

    error = raised.value
    assert error.code is ModelGatewayErrorCode.MODEL_TARGET_FORBIDDEN
    assert error.retryable is False
    assert error.message == "The model provider destination was refused."
    assert "api.openai.com" not in str(error)
    assert _SYNTHETIC_SECRET not in str(error)
    assert factory.built[0].transport.blocked_failure_code == TARGET_FORBIDDEN
    assert factory.built[0].backend.connect_calls == []
