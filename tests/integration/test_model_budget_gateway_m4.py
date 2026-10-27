"""Actual gateway attempt boundaries against a real isolated Run ledger."""

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.run_budget_ledger import RunBudgetAttempt, RunBudgetLedger
from packages.agent_runtime.runtime_service import AgentRunService
from packages.model_gateway.contracts import ModelMessage
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.run_budget_adapter import AttemptCostUpperBound, ModelAttemptBudgetError
from tests.integration.test_summary_budget_bridge_m4 import (
    _create_account,
    _seed_tenant,
)
from tests.integration.test_summary_budget_bridge_m4 import (
    db_resources as db_resources,
)
from tests.integration.test_summary_budget_bridge_m4 import (
    pytestmark as pytestmark,
)
from tests.unit.test_model_gateway_run_budget import (
    FakePriceSource,
    RecordingProvider,
    context_for,
    frozen_plan,
    request,
    response,
    setup_chain,
)
from tests.unit.test_runtime_rag_evidence_projection import _model_graph


@pytest.fixture(scope="session")
def migrated_database():
    # This dedicated ledger database was explicitly upgraded to 0045 before
    # the test; do not race unrelated, in-progress schema work.
    assert os.environ.get("AGENTHUB_TEST_DATABASE_URL") == (
        "postgresql+psycopg://agenthub:m2_local_test@127.0.0.1:15432/"
        "agenthub_run_budget_m4_20261028_v4"
    )


@pytest.mark.asyncio
async def test_real_ledger_reserves_fallback_attempts_and_fences_replay(db_resources):
    _, factory = db_resources
    tenant = await _seed_tenant(factory, label=f"gateway-ledger-{uuid4().hex}")
    ledger = RunBudgetLedger()
    await _create_account(factory, ledger, tenant, amount=Decimal("2"))
    _, repository, profiles = setup_chain()
    repository.credential.workspace_id = tenant.workspace_id
    for profile in profiles:
        profile.workspace_id = tenant.workspace_id
    context = context_for(tenant.workspace_id)
    events = []
    provider = RecordingProvider(events)
    provider.complete_outcomes = {
        "primary": [ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)],
        "fallback": [response("fallback")],
    }
    budget = RunBudgetAttemptAdapter(
        factory,
        workspace_id=tenant.workspace_id,
        run_id=tenant.run_id,
        call_identity="round-1",
        price_source=FakePriceSource(AttemptCostUpperBound(Decimal("1"), "CNY", "fixture")),
    )
    gateway = ModelGatewayService(repository, provider, attempt_budget=budget)
    result = await gateway.generate(context, profiles[0].id, request())
    assert result.content == "fallback"
    async with factory() as session:
        attempts = list(
            await session.scalars(
                select(RunBudgetAttempt)
                .where(RunBudgetAttempt.run_id == tenant.run_id)
                .order_by(RunBudgetAttempt.attempt_identity)
            )
        )
        account = await ledger.get_account(
            session, workspace_id=tenant.workspace_id, run_id=tenant.run_id
        )
    assert [(row.attempt_kind, row.status) for row in attempts] == [
        ("MAIN_MODEL", "UNKNOWN_OUTCOME"),
        ("FALLBACK", "RESERVED"),
    ]
    assert account.reserved_amount == Decimal("2")
    before = list(events)
    with pytest.raises(ModelAttemptBudgetError) as repeated:
        await gateway.generate(context, profiles[0].id, request())
    assert repeated.value.code == "RUN_BUDGET_ATTEMPT_REPLAYED"
    assert events == before
    service = AgentRunService(
        factory, model_attempt_budget_factory=lambda run, round_number: budget
    )
    completed = await service._complete_run(
        context,
        tenant.run_id,
        status="SUCCEEDED",
        final_output="fallback",
        failure_code=None,
        model_step_count=1,
        tool_call_count=0,
        usage_records=[],
    )
    assert completed.status == "NEEDS_ATTENTION"
    assert completed.failure_code == "RUN_BUDGET_RECONCILIATION_REQUIRED"
    assert completed.final_output == "fallback"


@pytest.mark.asyncio
async def test_real_budget_rejection_stops_before_fallback_dispatch(db_resources):
    _, factory = db_resources
    tenant = await _seed_tenant(factory, label=f"gateway-limit-{uuid4().hex}")
    ledger = RunBudgetLedger()
    await _create_account(factory, ledger, tenant, amount=Decimal("1"))
    _, repository, profiles = setup_chain()
    repository.credential.workspace_id = tenant.workspace_id
    for profile in profiles:
        profile.workspace_id = tenant.workspace_id
    events = []
    provider = RecordingProvider(events)
    provider.complete_outcomes = {
        "primary": [ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)],
        "fallback": [response("fallback")],
    }
    budget = RunBudgetAttemptAdapter(
        factory,
        workspace_id=tenant.workspace_id,
        run_id=tenant.run_id,
        call_identity="round-1",
        price_source=FakePriceSource(AttemptCostUpperBound(Decimal("1"), "CNY", "fixture")),
    )
    gateway = ModelGatewayService(repository, provider, attempt_budget=budget)
    with pytest.raises(ModelAttemptBudgetError) as rejected:
        await gateway.generate(context_for(tenant.workspace_id), profiles[0].id, request())
    assert rejected.value.code == "RUN_BUDGET_INSUFFICIENT_FUNDS"
    assert events == [("provider.complete", "primary")]
    async with factory() as session:
        account = await ledger.get_account(
            session, workspace_id=tenant.workspace_id, run_id=tenant.run_id
        )
    assert account.reserved_amount == Decimal("1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget_limit,expected",
    [(Decimal("1"), None), (Decimal("0.5"), "RUN_BUDGET_INSUFFICIENT_FUNDS")],
)
async def test_real_model_node_uses_the_run_ledger(db_resources, budget_limit, expected):
    _, factory = db_resources
    tenant = await _seed_tenant(factory, label=f"runtime-ledger-{uuid4().hex}")
    ledger = RunBudgetLedger()
    await _create_account(factory, ledger, tenant, amount=budget_limit)
    _, repository, profiles = setup_chain()
    repository.credential.workspace_id = tenant.workspace_id
    for profile in profiles:
        profile.workspace_id = tenant.workspace_id
    events = []
    provider = RecordingProvider(events)
    provider.complete_outcomes = {"primary": [response("primary")]}
    gateway = ModelGatewayService(repository, provider)
    graph = _model_graph(request_sink=[])
    async with factory() as session:
        graph.run = await session.get(AgentRun, tenant.run_id)
    graph.context = context_for(tenant.workspace_id)
    graph.service.session_factory = factory
    graph.service.model_gateway_factory = lambda session: gateway
    graph.service.model_attempt_budget_factory = lambda run, round_number: RunBudgetAttemptAdapter(
        factory,
        workspace_id=tenant.workspace_id,
        run_id=run.id,
        call_identity=f"round-{round_number}",
        price_source=FakePriceSource(AttemptCostUpperBound(Decimal("1"), "CNY", "fixture")),
    )
    state = {
        "messages": [
            ModelMessage("system", "runtime"),
            ModelMessage("system", "system"),
            ModelMessage("user", "question"),
        ],
        "spec": FrozenAgentSpec(
            model_plan=frozen_plan(profiles), system_prompt="system", prompt_version=1, runtime={}
        ),
        "runtime": {"max_steps": 5, "context_budget": graph._runtime_context_budget},
        "tool_definitions": {},
        "model_round_count": 0,
        "usage_records": [],
    }
    result = await graph.model(state)
    assert result.get("failure_code") == expected
    assert events == ([] if expected else [("provider.complete", "primary")])
    async with factory() as session:
        account = await ledger.get_account(
            session, workspace_id=tenant.workspace_id, run_id=tenant.run_id
        )
    assert account.reserved_amount == (Decimal("0") if expected else Decimal("1"))
    assert gateway.attempt_budget is None
