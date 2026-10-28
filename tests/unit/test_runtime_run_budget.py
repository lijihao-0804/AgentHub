"""The actual model node binds an explicitly supplied per-Run budget."""

import pytest

from packages.model_gateway.contracts import ModelMessage, ModelResponse
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.run_budget_adapter import ModelAttemptBudgetError
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec, _model_graph


class _Budget:
    def is_bound_to_run(self, workspace_id, run_id, call_identity):
        return call_identity == "round-1"

    async def reserve_attempt(self, *args, **kwargs):
        raise ModelAttemptBudgetError("RUN_BUDGET_INSUFFICIENT_FUNDS", "budget exhausted")

    async def mark_unknown_outcome(self, *args, **kwargs):
        return None


class _Gateway(ModelGatewayService):
    def __init__(self, captured):
        self.attempt_budget = None
        self.captured = captured

    async def prepare_resolved(self, context, plan, request, *, operation):
        self.captured.append(self.attempt_budget)
        return self

    async def generate_resolved(self, context, plan, request):
        await self.attempt_budget.reserve_attempt()
        return ModelResponse(content="unexpected", provider="fake", model="fake")


@pytest.mark.asyncio
async def test_model_node_binds_run_budget_and_reports_rejection_without_dispatch():
    graph = _model_graph(request_sink=[])
    bound = []
    calls = []
    gateway = _Gateway(bound)
    budget = _Budget()
    graph.service.model_gateway_factory = lambda session: gateway
    graph.service.model_attempt_budget_factory = lambda run, round_number: (
        calls.append((run.id, round_number)) or budget
    )
    state = {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            ModelMessage(role="user", content="question"),
        ],
        "spec": _frozen_spec(20_000),
        "runtime": {"max_steps": 5, "context_budget": graph._runtime_context_budget},
        "tool_definitions": {},
        "model_round_count": 0,
        "usage_records": [],
    }
    result = await graph.model(state)
    assert result["failure_code"] == "RUN_BUDGET_INSUFFICIENT_FUNDS"
    assert calls == [(graph.run.id, 1)]
    assert bound == [budget]
    assert gateway.attempt_budget is None


@pytest.mark.asyncio
async def test_configured_budget_rejects_gateway_that_cannot_enforce_it():
    graph = _model_graph(request_sink=[])
    graph.service.model_attempt_budget_factory = lambda run, round_number: _Budget()
    state = {
        "messages": [ModelMessage(role="user", content="question")],
        "spec": _frozen_spec(20_000),
        "runtime": {"max_steps": 5, "context_budget": graph._runtime_context_budget},
        "tool_definitions": {},
        "model_round_count": 0,
        "usage_records": [],
    }
    result = await graph.model(state)
    assert result["failure_code"] == "RUN_BUDGET_BINDING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_model_node_refuses_a_budget_bound_to_another_run():
    graph = _model_graph(request_sink=[])
    captured = []
    gateway = _Gateway(captured)

    class WrongRunBudget(_Budget):
        def is_bound_to_run(self, workspace_id, run_id, call_identity):
            return False

    graph.service.model_gateway_factory = lambda session: gateway
    graph.service.model_attempt_budget_factory = lambda run, round_number: WrongRunBudget()
    state = {
        "messages": [ModelMessage(role="user", content="question")],
        "spec": _frozen_spec(20_000),
        "runtime": {"max_steps": 5, "context_budget": graph._runtime_context_budget},
        "tool_definitions": {},
        "model_round_count": 0,
        "usage_records": [],
    }
    result = await graph.model(state)
    assert result["failure_code"] == "RUN_BUDGET_BINDING_UNAVAILABLE"
    assert captured == []
