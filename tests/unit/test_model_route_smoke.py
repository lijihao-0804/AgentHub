from __future__ import annotations

import socket
import sys
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest

import scripts.model_route as model_route
from packages.model_gateway.configured_route import ConfiguredRoute, Endpoint


class _FakeLiteLLM(ModuleType):
    class APIConnectionError(Exception):
        def __init__(
            self,
            *,
            message: str,
            llm_provider: str,
            model: str,
        ) -> None:
            super().__init__(message)
            self.llm_provider = llm_provider
            self.model = model

    def __init__(self) -> None:
        super().__init__("litellm")
        self.requests: list[dict[str, object]] = []

    async def acompletion(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("tools"):
            tool_call = SimpleNamespace(
                id="fake-tool-call",
                function=SimpleNamespace(
                    name="route_probe",
                    arguments='{"value":"ok"}',
                ),
            )
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
            finish_reason = "tool_calls"
        else:
            message = SimpleNamespace(content="OK", tool_calls=[])
            finish_reason = "stop"
        return SimpleNamespace(
            model="fake-provider-model",
            choices=[
                SimpleNamespace(
                    message=message,
                    finish_reason=finish_reason,
                )
            ],
            usage=None,
        )


def _route() -> ConfiguredRoute:
    capabilities = {
        "tool_calling": True,
        "streaming": True,
        "structured_output": False,
        "vision": False,
        "max_context_tokens": 32_768,
    }
    primary = Endpoint(
        name="primary",
        provider="openai-compatible",
        model="primary-model",
        base_url="http://127.0.0.1:3425/v1",
        api_key="primary-secret-never-recorded",
        temperature=Decimal("0"),
        max_tokens=2048,
        timeout_seconds=Decimal("20"),
        capabilities=capabilities,
    )
    fallback = Endpoint(
        name="fallback",
        provider="openai-compatible",
        model="fallback-model",
        base_url="https://provider.invalid/v1",
        api_key="fallback-secret-never-recorded",
        temperature=Decimal("0"),
        max_tokens=2048,
        timeout_seconds=Decimal("60"),
        capabilities=capabilities,
    )
    return ConfiguredRoute(1, "local-rag", primary, (fallback,))


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _all_keys(child)


@pytest.mark.asyncio
async def test_smoke_simulates_primary_failure_locally_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_litellm = _FakeLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("model route smoke attempted network I/O")

    route = _route()
    with monkeypatch.context() as network_guard:
        network_guard.setattr(socket, "create_connection", forbidden_network)
        network_guard.setattr(socket, "getaddrinfo", forbidden_network)
        network_guard.setattr(socket.socket, "connect", forbidden_network)
        network_guard.setattr(socket.socket, "connect_ex", forbidden_network)
        result = await model_route.smoke(route, SimpleNamespace(installed=None))

    assert result["passed"] is True
    assert [check["check"] for check in result["checks"]] == [
        "primary_chat",
        "primary_native_tools",
        "simulated_primary_connection_failure_failover",
        "primary_recovered_after_simulated_failure",
    ]
    assert result["checks"][2]["selected_model"] == route.fallbacks[0].model
    assert result["checks"][3]["selected_model"] == route.primary.model
    assert result["attempt_counts"] == {
        "SIMULATED_CONNECTION_FAILURE": 1,
        "LITELLM_DISPATCH": 4,
    }

    simulated, fallback, recovered = result["attempts"][2:5]
    assert simulated["endpoint"] == route.primary.base_url
    assert simulated["record_kind"] == "SIMULATED_CONNECTION_FAILURE"
    assert simulated["litellm_invoked"] is False
    assert simulated["provider_network_confirmed"] is False
    assert fallback["endpoint"] == route.fallbacks[0].base_url
    assert fallback["requested_model"] == "openai/fallback-model"
    assert fallback["record_kind"] == "LITELLM_DISPATCH"
    assert recovered["endpoint"] == route.primary.base_url
    assert recovered["requested_model"] == "openai/primary-model"
    assert recovered["record_kind"] == "LITELLM_DISPATCH"

    assert len(fake_litellm.requests) == 4
    assert [request["api_base"] for request in fake_litellm.requests] == [
        route.primary.base_url,
        route.primary.base_url,
        route.fallbacks[0].base_url,
        route.primary.base_url,
    ]
    assert all("client" in request for request in fake_litellm.requests)

    keys = set(_all_keys(result))
    assert "client" not in keys
    assert "api_key" not in keys
    assert "primary-secret-never-recorded" not in str(result)
    assert "fallback-secret-never-recorded" not in str(result)
