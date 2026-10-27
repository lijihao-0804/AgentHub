"""A priced trial never silently retries or bypasses admission."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from benchmarks.evaluation.memory_deepseek_v41_counter import DeepseekV41TextCounter
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import (
    PRICE_IDENTITY,
    MemoryTrialDispatchError,
    generate_once,
)
from packages.agent_runtime.context_budget import ContextBudgetPolicy, ContextCategory
from packages.model_gateway.contracts import ModelMessage
from tests.unit.test_context_policy_m2_runtime import _freeze, _profile


@pytest.fixture
def trial_dir():
    path = Path(".scratch/context-policy-m4-20261027/client-tests") / uuid4().hex
    path.mkdir(parents=True)
    return path


def _admitted():
    messages = (ModelMessage(role="user", content="public calibration"),)
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)))
    result = policy.admit(messages, categories=(ContextCategory.CURRENT_USER_TASK,))
    return messages, result


def _counter(tokens: int, *, verified: bool = True) -> DeepseekV41TextCounter:
    class FakeTokenizer:
        def encode(self, _text, *, add_special_tokens):
            assert add_special_tokens is False
            return SimpleNamespace(ids=range(tokens))

    counter = DeepseekV41TextCounter(FakeTokenizer())
    counter._verified_source = verified
    return counter


@pytest.mark.asyncio
async def test_one_successful_call_is_reserved_and_reasoning_is_not_persisted(trial_dir):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4.1-flash-1017",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
                "choices": [
                    {
                        "message": {"content": "ok", "reasoning_content": "private"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    messages, admission = _admitted()
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        result = await generate_once(
            budget=budget,
            admission=admission,
            original_messages=messages,
            attempt_id="sample-1",
            stage="M4",
            endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
            api_key="test-only",
            output_limit=64,
            output_directory=trial_dir / "results",
            transport=httpx.MockTransport(handler),
            provider_counter=_counter(20),
        )
        assert len(calls) == 1
        assert budget.state["attempts"][0]["status"] == "SETTLED"
        assert budget.state["attempts"][0]["estimated_input_tokens"] == 32768
        assert budget.state["attempts"][0]["input_counter"] == result["input_counter"]
        assert result["input_counter"]["tokens"] == 20
        assert Decimal(result["conservative_cost_cny"]) == Decimal("0.00012")
        assert result["cost_identity"] == PRICE_IDENTITY
        assert result["reasoning_tokens"] == 7
        assert result["cached_tokens"] is None and result["cache_write_tokens"] is None
        assert "private" not in (trial_dir / "results/sample-1.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_observed_zero_cached_tokens_is_not_unknown(trial_dir):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4.1-flash-1017",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    messages, admission = _admitted()
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        result = await generate_once(
            budget=budget,
            admission=admission,
            original_messages=messages,
            attempt_id="zero-cache",
            stage="M5",
            endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
            api_key="test-only",
            output_limit=64,
            output_directory=trial_dir / "results",
            transport=httpx.MockTransport(handler),
        )
    assert result["cached_tokens"] == 0
    assert result["cache_write_tokens"] is None


@pytest.mark.asyncio
async def test_thinking_override_is_only_allowed_in_calibration(trial_dir):
    messages, admission = _admitted()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4.1-flash-1017",
                "usage": {"prompt_tokens": 20, "completion_tokens": 3},
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        args = dict(
            budget=budget,
            admission=admission,
            original_messages=messages,
            endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
            api_key="test-only",
            output_limit=64,
            output_directory=trial_dir / "results",
            transport=httpx.MockTransport(handler),
            enable_thinking=False,
        )
        with pytest.raises(ValueError, match="NOT_CALIBRATION"):
            await generate_once(attempt_id="forbidden", stage="M4", **args)
        result = await generate_once(attempt_id="allowed", stage="CALIBRATION", **args)
    assert requests[0]["enable_thinking"] is False
    assert result["enable_thinking"] is False
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_unconfirmed_dispatch_keeps_reservation_and_blocks_next_call(trial_dir):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("provider outcome unknown")

    messages, admission = _admitted()
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        args = dict(
            budget=budget,
            admission=admission,
            original_messages=messages,
            stage="M4",
            endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
            api_key="test-only",
            output_limit=64,
            output_directory=trial_dir / "results",
            transport=httpx.MockTransport(handler),
            provider_counter=_counter(20),
        )
        with pytest.raises(MemoryTrialDispatchError):
            await generate_once(attempt_id="first", **args)
        with pytest.raises(ValueError, match="UNRESOLVED"):
            await generate_once(attempt_id="second", **args)
        assert len(calls) == 1
        assert budget.state["attempts"][0]["status"] == "RESERVED_OR_UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("counter", [None, _counter(20, verified=False), _counter(32768)])
async def test_m4_never_dispatches_without_verified_counter_or_when_it_exceeds_window(
    trial_dir, counter
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    messages, admission = _admitted()
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        with pytest.raises(ValueError, match="COUNTER_REQUIRED|WINDOW_EXCEEDED"):
            await generate_once(
                budget=budget,
                admission=admission,
                original_messages=messages,
                attempt_id="blocked",
                stage="M4",
                endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
                api_key="test-only",
                output_limit=64,
                output_directory=trial_dir / "results",
                provider_counter=counter,
                transport=httpx.MockTransport(handler),
            )
        assert budget.state["attempts"] == []
    assert calls == []


@pytest.mark.asyncio
async def test_m4_does_not_dispatch_at_the_real_hard_pressure_boundary(trial_dir):
    messages, admission = _admitted()
    limit = (85 * admission.usage.available_input - 1) // 100
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        with pytest.raises(ValueError, match="WINDOW_EXCEEDED"):
            await generate_once(
                budget=budget,
                admission=admission,
                original_messages=messages,
                attempt_id="hard-boundary",
                stage="M4",
                endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
                api_key="test-only",
                output_limit=64,
                output_directory=trial_dir / "results",
                provider_counter=_counter(limit + 1),
                provider_input_limit=limit,
            )
        assert budget.state["attempts"] == []


@pytest.mark.asyncio
async def test_input_changed_after_admission_never_reserves_or_dispatches(trial_dir):
    messages, admission = _admitted()
    changed = (ModelMessage(role="user", content="different input"),)
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        with pytest.raises(ValueError, match="NOT_FULLY_ADMITTED"):
            await generate_once(
                budget=budget,
                admission=admission,
                original_messages=changed,
                attempt_id="bad",
                stage="M4",
                endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
                api_key="test-only",
                output_limit=64,
                output_directory=trial_dir / "results",
            )
        assert budget.state["attempts"] == []


@pytest.mark.asyncio
async def test_legacy_window_without_adaptive_policy_is_not_accepted(trial_dir):
    messages = (ModelMessage(role="user", content="public calibration"),)
    legacy = ContextBudgetPolicy(profiles=(_profile(total=32768),))
    admission = legacy.admit(messages, categories=(ContextCategory.CURRENT_USER_TASK,))
    with MemoryTrialBudget(trial_dir / "budget.json", price_identity=PRICE_IDENTITY) as budget:
        with pytest.raises(ValueError, match="NOT_FULLY_ADMITTED"):
            await generate_once(
                budget=budget,
                admission=admission,
                original_messages=messages,
                attempt_id="legacy",
                stage="M4",
                endpoint="https://demo.cn-beijing.maas.aliyuncs.com/v1",
                api_key="test-only",
                output_limit=64,
                output_directory=trial_dir / "results",
            )
        assert budget.state["attempts"] == []
