from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.evaluation.scenario_runner import ScenarioEvaluationDriver
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import ScriptedGateway, _seed
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database

__all__ = ["db_factory", "migrated_database"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]


@pytest.mark.asyncio
async def test_scenarios_use_real_turn_history_pinned_identity_and_independent_threads(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-{uuid4().hex}")
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run"}}
    )
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="Please provide your customer reference.", provider="fake", model="fake"
            ),
            ModelResponse(
                content="The customer reference is recorded.", provider="fake", model="fake"
            ),
        ]
    )
    runtime = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        thread_context_provider=SqlAlchemyThreadContextProvider(db_factory),
    )
    driver = ScriptedScenarioDriver(runtime, driver_kind="controlled")
    arguments = dict(
        case_id="clarification",
        agent_version_id=base["version"].id,
        user_turns=["Help with my account.", "My reference is local-fixture."],
        knowledge_snapshots=[],
    )
    first = await driver.execute(context, **arguments)
    second = await driver.execute(context, **arguments)
    assert first["thread_id"] != second["thread_id"]
    assert first["execution_complete"] and first["executed_turn_count"] == 2
    assert len({r["run_id"] for r in first["turns"] + second["turns"]}) == 4
    assert "local-fixture" not in str(first)
    assert "Please provide" in str(gateway.requests[1].messages)
    async with db_factory() as session:
        rows = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.thread_id.in_([first["thread_id"], second["thread_id"]])
                )
            )
        )
        assert len(rows) == 4
        assert all(row.agent_version_id == base["version"].id for row in rows)
        assert all(row.resolved_spec_hash == base["version"].resolved_spec_hash for row in rows)


@pytest.mark.asyncio
async def test_scenario_does_not_elevate_permissions_or_accept_another_workspace(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-auth-{uuid4().hex}")
    driver = ScriptedScenarioDriver(AgentRunService(db_factory), driver_kind="controlled")
    arguments = dict(
        case_id="scope",
        agent_version_id=base["version"].id,
        user_turns=["q"],
        knowledge_snapshots=[],
    )
    with pytest.raises(AgentHubError) as denied:
        await driver.execute(base["context"], **arguments)
    assert denied.value.status_code == 403
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run"}}
    )
    with pytest.raises(AgentHubError) as other:
        await driver.execute(context.model_copy(update={"workspace_id": str(uuid4())}), **arguments)
    assert other.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("decision, count", [("APPROVED", 1), ("DENIED", 0), (None, 0)])
async def test_scripted_approval_uses_persisted_decision_and_never_infers_consent(
    db_factory, decision, count
):
    spec = {
        "kind": "builtin",
        "identity": "create_ticket",
        "description": "Controlled ticket",
        "input_schema": {
            "type": "object",
            "properties": {"customer_ref": {"type": "string"}, "subject": {"type": "string"}},
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
    }
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-approval-{uuid4().hex}", tool_spec=spec)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="local", name="fixture")
        )
        await session.commit()
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="fake",
                tool_calls=(
                    ModelToolCall("create_ticket", {"customer_ref": "local", "subject": "fixture"}),
                ),
            ),
            ModelResponse(content="controlled final", provider="fake", model="fake"),
        ]
    )
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run", "approve_action"}}
    )
    runtime = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(os.environ["AGENTHUB_TEST_DATABASE_URL"]),
    )
    observation = await ScriptedScenarioDriver(runtime, driver_kind="controlled").execute(
        context,
        case_id="approval",
        agent_version_id=base["version"].id,
        user_turns=["Open a local fixture ticket."],
        knowledge_snapshots=[],
        approval_decisions=[decision] if decision else [],
    )
    async with db_factory() as session:
        actual = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.workspace_id == base["workspace_id"])
        )
        assert actual == count
    assert observation["approval_decision_count"] == (1 if decision else 0)
    assert observation["execution_complete"] is (decision is not None)
    if decision is None:
        assert observation["stop_reason"] == "approval_script_exhausted"
        assert observation["turns"][0]["status"] == "WAITING_APPROVAL"


@pytest.mark.asyncio
async def test_formal_scenario_links_first_run_before_model_and_checks_persisted_facts(db_factory):
    from benchmarks.evaluation.reviewed_support_data import formal_items

    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-formal-{uuid4().hex}")
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run"}}
    )
    gateway = ScriptedGateway(
        [ModelResponse(content="fixture final", provider="fake", model="fake")]
    )
    runtime = AgentRunService(db_factory, model_gateway_factory=lambda session: gateway)
    scenarios = ScriptedScenarioDriver(runtime, driver_kind="controlled")

    async def context_factory(run):
        return context

    async def fixture_factory(ctx):
        async with db_factory() as session:
            customer = Customer(
                workspace_id=base["workspace_id"], customer_ref=uuid4().hex, name="fixture"
            )
            session.add(customer)
            await session.commit()
            return customer.id

    driver = ScenarioEvaluationDriver(scenarios, context_factory, fixture_factory)
    raw = formal_items()[0]
    item = SimpleNamespace(**raw)
    variant = SimpleNamespace(agent_version_id=base["version"].id, effective_knowledge_snapshots=[])
    prepared = await driver.prepare(
        run=SimpleNamespace(workspace_id=base["workspace_id"]), variant=variant, item=item
    )
    assert not gateway.requests
    async with db_factory() as session:
        persisted = await session.get(AgentRun, prepared.agent_run_id)
        assert persisted is not None and persisted.thread_id is not None
    result = await prepared.execute()
    assert result.agent_run_id == prepared.agent_run_id
    assert result.observation["business_outcome"]["success"] is True
    assert result.observation["business_outcome"]["ticket_count"] == 0
    assert result.observation["steps"] == []
    assert raw["expected"]["scenario"]["reference_answer"] not in str(gateway.requests)


@pytest.mark.asyncio
async def test_prepared_scenario_rejects_changed_input_without_model_call(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-prepared-{uuid4().hex}")
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run"}}
    )
    gateway = ScriptedGateway([])
    driver = ScriptedScenarioDriver(
        AgentRunService(db_factory, model_gateway_factory=lambda session: gateway),
        driver_kind="controlled",
    )
    handle = await driver.prepare_first(
        context, agent_version_id=base["version"].id, first_input="original", knowledge_snapshots=[]
    )
    with pytest.raises(RuntimeError, match="IDENTITY_MISMATCH"):
        await driver.execute(
            context,
            case_id="changed",
            agent_version_id=base["version"].id,
            user_turns=["different"],
            knowledge_snapshots=[],
            prepared_first=handle,
        )
    assert not gateway.requests


@pytest.mark.asyncio
async def test_formal_dataset_freezes_driver_and_recovery_does_not_infer_whole_scenario_success(
    db_factory,
):
    from benchmarks.evaluation.reviewed_support_data import formal_items
    from packages.evaluation.build_identity import StaticBuildIdentityProvider
    from packages.evaluation.experiments import ExperimentService
    from packages.evaluation.models import EvaluationExperimentCaseResult
    from packages.evaluation.runner import ExperimentRunner
    from packages.evaluation.service import EvaluationDatasetService
    from tests.integration.test_m7b_experiments import _manager_context, _pricing

    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-recovery-{uuid4().hex}")
        context = _manager_context(base)
        datasets = EvaluationDatasetService()
        dataset = await datasets.create_dataset(session, context=context, name=uuid4().hex)
        items = [formal_items()[0]]
        with pytest.raises(AgentHubError, match="schema_version 2"):
            await datasets.create_version(
                session, context=context, dataset_id=dataset.id, items=items
            )
        version = await datasets.create_version(
            session, context=context, dataset_id=dataset.id, items=items, schema_version=2
        )
        await datasets.publish_version(
            session, context=context, dataset_id=dataset.id, version_id=version.id
        )
        pricing = await _pricing(session, base)
        service = ExperimentService(StaticBuildIdentityProvider("1" * 40))
        experiment = await service.create_experiment(
            session,
            context=context,
            name=uuid4().hex,
            description=None,
            dataset_version_id=version.id,
            split="DEV",
            purpose="DEVELOPMENT",
        )
        assert experiment.evaluator_manifest["scenario"]["driver_version"] == "scripted-scenario-v2"
        await service.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label="controlled",
            agent_version_id=base["version"].id,
            pricing_snapshot_id=pricing.id,
            ordinal=0,
        )
        await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
        run, _ = await service.create_run(session, context=context, experiment_id=experiment.id)
    gateway = ScriptedGateway([ModelResponse(content="controlled", provider="fake", model="fake")])
    driver = ScriptedScenarioDriver(
        AgentRunService(db_factory, model_gateway_factory=lambda session: gateway),
        driver_kind="controlled",
    )
    observed = await driver.execute(
        context,
        case_id="first-turn-only",
        agent_version_id=base["version"].id,
        user_turns=["first turn"],
        knowledge_snapshots=[],
    )
    runner = ExperimentRunner(db_factory)
    async with db_factory() as session:
        await runner.prepare_run(session, run_id=run.id)
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.experiment_run_id == run.id
            )
        )
        case.status = "RUNNING"
        case.agent_run_id = UUID(observed["turns"][0]["run_id"])
        await session.commit()
        await runner.recover_inflight_cases(session, run_id=run.id)
        await session.refresh(case)
        assert case.status == "FAILED"
        assert case.failure_code == "EVALUATION_SCENARIO_RECOVERY_REQUIRED"
