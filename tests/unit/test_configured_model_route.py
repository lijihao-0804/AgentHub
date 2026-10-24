from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.configured_route import (
    ConfiguredRoute,
    ConfiguredRouteError,
    Endpoint,
    build_gateway,
    load_route,
)
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    RetryPolicy,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode

ROUTE_PATH = Path("benchmarks/public_rag/model-route.json")
ROUTE_SOURCE = ROUTE_PATH.read_text(encoding="utf-8")


def test_route_loader_uses_default_loopback_and_external_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = json.loads(ROUTE_SOURCE)
    assert source["retry_policy"] == {"max_attempts": 1}
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda _self, **_kwargs: ROUTE_SOURCE,
    )
    monkeypatch.setattr(Path, "exists", lambda _self: False)

    route = load_route(
        ROUTE_PATH,
        env={
            "AGENTHUB_RAG_BASE_URL": "https://provider.invalid/v1",
            "AGENTHUB_RAG_API_KEY": "fallback-secret",
        },
    )

    assert route.name == "local-rag"
    assert route.primary.base_url == "http://127.0.0.1:3425/v1"
    assert route.primary.model == "auto/deepseek-v4.1-flash"
    assert route.primary.api_key == "magpie"
    assert route.primary.timeout_seconds == Decimal("20")
    assert route.primary.max_tokens == 2048
    assert route.fallbacks[0].base_url == "https://provider.invalid/v1"
    assert route.fallbacks[0].api_key == "fallback-secret"
    assert route.fallbacks[0].timeout_seconds == Decimal("60")
    assert "fallback-secret" not in repr(route.fallbacks[0])


def test_nonempty_environment_override_wins_and_empty_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = json.loads(ROUTE_SOURCE)
    monkeypatch.setattr(Path, "read_text", lambda _self, **_kwargs: json.dumps(source))
    monkeypatch.setattr(
        Path,
        "exists",
        lambda _self: True,
    )
    monkeypatch.setattr(
        "packages.model_gateway.configured_route.dotenv_values",
        lambda *_args, **_kwargs: {
            "AGENTHUB_MAGPIE_API_KEY": "file-key",
            "AGENTHUB_RAG_BASE_URL": "https://file.invalid/v1",
            "AGENTHUB_RAG_API_KEY": "file-fallback",
        },
    )

    route = load_route(
        ROUTE_PATH,
        env={
            "AGENTHUB_MAGPIE_API_KEY": "",
            "AGENTHUB_RAG_BASE_URL": "https://process.invalid/v1",
            "AGENTHUB_RAG_API_KEY": "",
        },
    )

    assert route.primary.api_key == "file-key"
    assert route.fallbacks[0].base_url == "https://process.invalid/v1"
    assert route.fallbacks[0].api_key == "file-fallback"


def test_missing_external_credentials_are_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = json.loads(ROUTE_SOURCE)
    monkeypatch.setattr(Path, "read_text", lambda _self, **_kwargs: json.dumps(source))
    monkeypatch.setattr(Path, "exists", lambda _self: False)

    with pytest.raises(ConfiguredRouteError) as raised:
        load_route(
            ROUTE_PATH,
            env={"AGENTHUB_RAG_BASE_URL": "https://provider.invalid/v1"},
        )

    assert "fallback 1 api_key is missing" in str(raised.value)
    assert "secret" not in str(raised.value).lower()


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.invalid/v1",
        "https://user:password@provider.invalid/v1",
        "https://user%40name@provider.invalid/v1",
        "https://provider.invalid/v1?key=secret",
        "https://provider.invalid/v1#secret",
        "https://provider.invalid/v1?",
        "https://provider.invalid/v1#",
    ],
)
def test_external_url_rules_are_not_relaxed(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = json.loads(ROUTE_SOURCE)
    source["fallbacks"][0]["base_url_env"] = None
    source["fallbacks"][0]["base_url"] = url
    monkeypatch.setattr(Path, "read_text", lambda _self, **_kwargs: json.dumps(source))
    monkeypatch.setattr(Path, "exists", lambda _self: False)

    with pytest.raises(ConfiguredRouteError) as raised:
        load_route(ROUTE_PATH, env={"AGENTHUB_RAG_API_KEY": "not-for-output"})

    assert "secret" not in str(raised.value)
    assert "not-for-output" not in str(raised.value)


def _route(api_key: str = "fallback-secret") -> ConfiguredRoute:
    primary = Endpoint(
        name="local",
        provider="openai-compatible",
        model="auto/deepseek-v4.1-flash",
        base_url="http://127.0.0.1:3425/v1",
        api_key="magpie",
        temperature=Decimal("0"),
        max_tokens=2048,
        timeout_seconds=Decimal("20"),
        capabilities={
            "tool_calling": True,
            "streaming": True,
            "structured_output": False,
            "vision": False,
            "max_context_tokens": 32768,
        },
    )
    fallback = Endpoint(
        name="external",
        provider="openai-compatible",
        model="deepseek-v4.1-flash-1017",
        base_url="https://provider.invalid/v1",
        api_key=api_key,
        temperature=Decimal("0"),
        max_tokens=2048,
        timeout_seconds=Decimal("60"),
        capabilities=dict(primary.capabilities),
    )
    return ConfiguredRoute(1, "local-rag", primary, (fallback,))


def _context(workspace_id: UUID | None = None) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="route-test", trace_id="route-test"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id or uuid4()),
        workspace_role="DEVELOPER",
    )


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        retry_policy=RetryPolicy(max_attempts=1),
    )


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []
        self.outcomes: dict[str, list[str]] = {}

    async def complete(self, profile, credential, request) -> ModelResponse:
        del request
        self.calls.append(
            {
                "model": profile.model,
                "base_url": credential.base_url,
                "workspace_id": profile.workspace_id,
            }
        )
        outcome = self.outcomes[profile.model].pop(0)
        if outcome == "connection-failure":
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        return ModelResponse(
            content="answer",
            provider=credential.provider,
            model=profile.model,
        )

    async def stream(self, profile, credential, request) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.calls.append(
            {
                "model": profile.model,
                "base_url": credential.base_url,
                "workspace_id": profile.workspace_id,
            }
        )
        outcome = self.outcomes[profile.model].pop(0)
        if outcome == "connection-failure":
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        if outcome == "visible-failure":
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta="partial",
            )
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta="answer",
        )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=ModelResponse(
                content="answer",
                provider=credential.provider,
                model=profile.model,
            ),
        )

    async def health(self, profile, credential) -> None:
        del profile, credential


@pytest.mark.asyncio
async def test_repository_is_workspace_scoped_and_factory_is_transient() -> None:
    adapter = _Adapter()
    adapter.outcomes["auto/deepseek-v4.1-flash"] = ["success"]
    workspace_id = uuid4()
    context = _context(workspace_id)
    gateway, primary_id = build_gateway(_route(), context, adapter=adapter)

    repository = gateway.repository
    profile = await repository.get_model_profile(_context(workspace_id), primary_id)
    assert profile is not None
    assert await repository.get_model_profile(_context(), primary_id) is None
    assert (
        await repository.get_provider_credential(
            _context(workspace_id), profile.provider_credential_id
        )
        is not None
    )
    assert (
        await repository.get_provider_credential(_context(), profile.provider_credential_id) is None
    )
    result = await gateway.generate(context, primary_id, _request())

    assert result.model == "auto/deepseek-v4.1-flash"
    assert adapter.calls == [
        {
            "model": "auto/deepseek-v4.1-flash",
            "base_url": "http://127.0.0.1:3425/v1",
            "workspace_id": workspace_id,
        }
    ]


@pytest.mark.asyncio
async def test_primary_success_does_not_use_fallback() -> None:
    adapter = _Adapter()
    adapter.outcomes["auto/deepseek-v4.1-flash"] = ["success"]
    context = _context()
    gateway, primary_id = build_gateway(_route(), context, adapter=adapter)

    await gateway.generate(context, primary_id, _request())
    assert [call["model"] for call in adapter.calls] == ["auto/deepseek-v4.1-flash"]


@pytest.mark.asyncio
async def test_connection_failure_uses_fallback_with_single_attempt() -> None:
    adapter = _Adapter()
    adapter.outcomes["auto/deepseek-v4.1-flash"] = ["connection-failure"]
    adapter.outcomes["deepseek-v4.1-flash-1017"] = ["success"]
    context = _context()
    gateway, primary_id = build_gateway(_route(), context, adapter=adapter)

    result = await gateway.generate(context, primary_id, _request())

    assert result.model == "deepseek-v4.1-flash-1017"
    assert [call["model"] for call in adapter.calls] == [
        "auto/deepseek-v4.1-flash",
        "deepseek-v4.1-flash-1017",
    ]


@pytest.mark.asyncio
async def test_visible_first_chunk_failure_does_not_fallback() -> None:
    adapter = _Adapter()
    adapter.outcomes["auto/deepseek-v4.1-flash"] = ["visible-failure"]
    context = _context()
    gateway, primary_id = build_gateway(_route(), context, adapter=adapter)
    received: list[ModelStreamEvent] = []

    with pytest.raises(ModelGatewayError) as raised:
        async for event in gateway.stream(context, primary_id, _request()):
            received.append(event)

    assert raised.value.code == ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
    assert [call["model"] for call in adapter.calls] == ["auto/deepseek-v4.1-flash"]
    assert received[0].message_delta == "partial"
