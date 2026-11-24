"""Unit tests for explicit prompt cache breakpoints and cache_hit_rate metrics."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime_projection import _aggregate_usage, _run_result
from packages.model_gateway.adapters.litellm.adapter import (
    _normalize_usage,
    _request_payload,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
    ModelUsage,
)


def _fake_profile(*, explicit_prompt_cache: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        model="deepseek-v4.1-flash-1017",
        temperature=Decimal("0.0"),
        max_tokens=512,
        timeout_seconds=Decimal("30"),
        capabilities=ModelCapabilities(
            tool_calling=True,
            explicit_prompt_cache=explicit_prompt_cache,
        ),
    )


def _fake_credential() -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai-compatible",
        secret="test-secret",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )


def _sample_request() -> ModelRequest:
    return ModelRequest(
        messages=(
            ModelMessage(role="system", content="You are an enterprise assistant."),
            ModelMessage(role="user", content="Find policy A."),
            ModelMessage(role="assistant", content="Searching policy A."),
            ModelMessage(role="user", content="Now compare with policy B."),
        ),
        tools=(
            ModelToolDefinition(
                name="calculate",
                description="Calculator",
                parameters={"type": "object", "properties": {}},
            ),
            ModelToolDefinition(
                name="search_knowledge",
                description="Search KB",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ),
    )


def test_default_profile_omits_explicit_cache_control() -> None:
    payload = _request_payload(
        _fake_profile(explicit_prompt_cache=False),
        _fake_credential(),
        _sample_request(),
    )
    assert payload["messages"][0]["content"] == "You are an enterprise assistant."
    assert payload["messages"][2]["content"] == "Searching policy A."
    assert "cache_control" not in payload["tools"][-1]


def test_explicit_prompt_cache_marks_tools_system_and_prior_turn() -> None:
    payload = _request_payload(
        _fake_profile(explicit_prompt_cache=True),
        _fake_credential(),
        _sample_request(),
    )
    assert "cache_control" not in payload["tools"][0]
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][0]["content"] == [
        {
            "type": "text",
            "text": "You are an enterprise assistant.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert payload["messages"][1]["content"] == "Find policy A."
    assert payload["messages"][2]["content"] == [
        {
            "type": "text",
            "text": "Searching policy A.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert payload["messages"][3]["content"] == "Now compare with policy B."


def test_normalize_usage_supports_anthropic_cache_read_input_tokens() -> None:
    usage = _normalize_usage(
        {
            "prompt_tokens": 2000,
            "completion_tokens": 100,
            "total_tokens": 2100,
            "cache_read_input_tokens": 1500,
        }
    )
    assert usage is not None
    assert usage.cached_tokens == 1500
    assert usage.cache_hit_rate == pytest.approx(0.75)


def test_model_usage_cache_hit_rate_handles_none_and_zero_input() -> None:
    assert ModelUsage(input_tokens=100, output_tokens=10, total_tokens=110).cache_hit_rate is None
    assert (
        ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0, cached_tokens=0).cache_hit_rate
        is None
    )
    assert (
        ModelUsage(
            input_tokens=1000, output_tokens=50, total_tokens=1050, cached_tokens=812
        ).cache_hit_rate
        == pytest.approx(0.812)
    )


def test_aggregate_usage_and_run_result_compute_cache_hit_rate() -> None:
    records = [
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
                "cached_tokens": 0,
            }
        },
        {
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 80,
                "total_tokens": 1280,
                "cached_tokens": 1000,
            }
        },
    ]
    agg = _aggregate_usage(records)
    assert agg["total_input_tokens"] == 2200
    assert agg["total_cached_tokens"] == 1000
    assert agg["cache_hit_rate"] == pytest.approx(round(1000 / 2200, 4))

    fake_run = SimpleNamespace(
        id=uuid4(),
        agent_version_id=uuid4(),
        status="SUCCEEDED",
        final_output="ok",
        failure_code=None,
        model_step_count=2,
        tool_call_count=1,
        total_input_tokens=2200,
        total_output_tokens=180,
        total_tokens=2380,
        total_cached_tokens=1000,
        total_cost_amount=None,
        cost_currency=None,
    )
    res = _run_result(fake_run)
    assert res.cache_hit_rate == pytest.approx(round(1000 / 2200, 4))
