from decimal import Decimal
from types import SimpleNamespace

import pytest

from benchmarks.evaluation.budget import TrialBudget
from benchmarks.evaluation.stream_budget import BudgetedStreamingAdapter
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [True, False])
async def test_stream_preserves_visibility_and_reserves_unknown_cost(tmp_path, monkeypatch, finish):
    budget = TrialBudget(tmp_path / "ledger.json", limit_cny=Decimal("50"), price_identity="test")
    closed = []

    async def events(*args):
        assert budget.allocated == Decimal("0.0704")
        try:
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA, message_delta="visible"
            )
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.COMPLETED,
                response=ModelResponse(
                    content="visible",
                    provider="deepseek",
                    model="deepseek-flash",
                    usage=ModelUsage(100, 20, 120),
                ),
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(LiteLLMProviderAdapter, "stream", events)
    adapter = BudgetedStreamingAdapter(budget)
    stream = adapter.stream(
        SimpleNamespace(model="deepseek-flash", max_tokens=800),
        None,
        ModelRequest((ModelMessage("user", "fixture"),)),
    )
    assert (await anext(stream)).message_delta == "visible"
    assert budget.allocated == Decimal("0.0704")
    if finish:
        final = await anext(stream)
        assert final.response.cost_estimate.currency == "CNY"
    await stream.aclose()
    assert closed == [True]
    assert budget.allocated == (Decimal("0.00036") if finish else Decimal("0.0704"))
    assert adapter.halted is (not finish)
