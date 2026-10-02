from decimal import Decimal
from types import SimpleNamespace

import pytest

from benchmarks.evaluation.budget import TrialBudget
from benchmarks.evaluation.live_pilot import PRICE_ID, BudgetedAdapter, security_cases
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import ModelMessage, ModelRequest, ModelResponse, ModelUsage


@pytest.mark.asyncio
async def test_provider_attempt_is_reserved_before_dispatch_and_priced_in_cny(
    tmp_path, monkeypatch
):
    budget = TrialBudget(tmp_path / "budget.json", limit_cny=Decimal("5"), price_identity=PRICE_ID)

    async def complete(*args):
        assert budget.allocated == Decimal("0.0704")
        return ModelResponse(
            content="answer",
            provider="deepseek",
            model="deepseek-flash",
            usage=ModelUsage(100, 20, 120),
        )

    monkeypatch.setattr(LiteLLMProviderAdapter, "complete", complete)
    adapter = BudgetedAdapter(budget)
    response = await adapter.complete(
        SimpleNamespace(model="deepseek-flash", max_tokens=800),
        None,
        ModelRequest((ModelMessage("user", "question"),)),
    )
    assert budget.allocated == Decimal("0.00036")
    assert response.cost_estimate.currency == "CNY"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_unknown_provider_cost_keeps_full_reserve(tmp_path, monkeypatch):
    budget = TrialBudget(tmp_path / "budget.json", limit_cny=Decimal("5"), price_identity=PRICE_ID)

    async def complete(*args):
        return ModelResponse(content="answer", provider="deepseek", model="deepseek-flash")

    monkeypatch.setattr(LiteLLMProviderAdapter, "complete", complete)
    with pytest.raises(ValueError, match="USAGE_UNKNOWN"):
        await BudgetedAdapter(budget).complete(
            SimpleNamespace(model="deepseek-flash", max_tokens=800),
            None,
            ModelRequest((ModelMessage("user", "question"),)),
        )
    assert budget.allocated == Decimal("0.0704")


@pytest.mark.asyncio
async def test_wrong_model_fails_before_network_or_reservation(tmp_path):
    budget = TrialBudget(tmp_path / "budget.json", limit_cny=Decimal("5"), price_identity=PRICE_ID)
    with pytest.raises(ValueError, match="BOUND_EXCEEDED"):
        await BudgetedAdapter(budget).complete(
            SimpleNamespace(model="deepseek-chat", max_tokens=800),
            None,
            ModelRequest((ModelMessage("user", "question"),)),
        )
    assert budget.allocated == 0


def test_security_has_distinct_targets_and_normal_write_controls():
    cases = security_cases()
    assert len({c["case_id"] for c in cases}) == 10
    assert sum(c.get("attack", False) for c in cases) == 6
    assert sum(c.get("attack_entry") == "tool_output" for c in cases) == 3
    assert all(c["expected_ticket_count"] == 0 for c in cases if c.get("attack"))
    assert any(c["expected_ticket_count"] == 1 for c in cases if not c.get("attack"))
