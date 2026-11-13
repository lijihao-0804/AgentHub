"""AE-6b contract and PostgreSQL environment tests."""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.environment import EnvHandle, SupportFixtureEnvironment
from packages.evaluation.interactive_driver import InteractiveScenarioDriver
from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity
from packages.evaluation.metrics import MetricStatus, evaluate_interactive
from packages.evaluation.models import InteractiveEpisode
from packages.evaluation.reproducibility import default_evaluator_manifest
from packages.evaluation.user_simulator import ScriptedUserSimulator, Turn
from packages.evaluation.validation import validate_dataset_item
from packages.tools.models import Customer
from tests.integration.test_m4c_agent_runtime import _seed
from tests.support.ae6b_lifecycle import create_owned_lifecycle_cases


@pytest_asyncio.fixture
async def ae6b_db_factory():
    url = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("PostgreSQL required")
    engine, factory = create_database(url)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_support_environment_reset_assert_teardown_and_isolation(
    ae6b_db_factory,
) -> None:
    async with ae6b_db_factory() as session:
        first = await _seed(session, label=f"ae6b-env-{uuid4().hex}")
        second = await _seed(session, label=f"ae6b-env-{uuid4().hex}")
    env = SupportFixtureEnvironment(ae6b_db_factory, workspace_id=str(first["workspace_id"]))
    other = SupportFixtureEnvironment(ae6b_db_factory, workspace_id=str(second["workspace_id"]))

    handle = await env.reset(seed=1, fixture_ref="support_customer")
    other_handle = await other.reset(seed=1, fixture_ref="support_customer")
    assert handle.workspace_id == str(first["workspace_id"])
    assert other_handle.workspace_id == str(second["workspace_id"])

    state = await env.assert_state(handle, {"ticket_count": 0})
    assert state.passed is True
    assert state.observed["ticket_count"] == 0

    with pytest.raises(PermissionError):
        await env.assert_state(replace(handle, workspace_id=str(second["workspace_id"])), {})
    await env.teardown(handle)
    await env.teardown(handle)  # idempotent


@pytest.mark.asyncio
async def test_support_environment_teardown_failure_is_observable() -> None:
    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("injected teardown failure")

    env = SupportFixtureEnvironment(BrokenFactory(), workspace_id=str(uuid4()))
    handle = EnvHandle("x" * 24, env.workspace_id, 0, "h")
    env._active[handle.instance_id] = handle
    with pytest.raises(RuntimeError):
        await env.teardown(handle)


@pytest.mark.asyncio
async def test_teardown_failure_keeps_handle_for_retry(ae6b_db_factory) -> None:
    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("injected teardown failure")

    async with ae6b_db_factory() as session:
        base = await _seed(session, label=f"ae6b-env-{uuid4().hex}")
    env = SupportFixtureEnvironment(ae6b_db_factory, workspace_id=str(base["workspace_id"]))
    handle = await env.reset(seed=1, fixture_ref="support_customer")
    working_factory = env.session_factory
    env.session_factory = BrokenFactory()
    with pytest.raises(RuntimeError):
        await env.teardown(handle)
    assert handle.instance_id in env._active
    env.session_factory = working_factory
    await env.teardown(handle)
    assert handle.instance_id not in env._active
    await env.teardown(handle)


@pytest.mark.asyncio
async def test_prepare_failure_cleans_reset_environment(ae6b_db_factory) -> None:
    class FailingScenarios:
        runtime = SimpleNamespace(session_factory=ae6b_db_factory)

        async def prepare_first(self, *args, **kwargs):
            raise RuntimeError("injected preparation failure")

    owned = (await create_owned_lifecycle_cases(ae6b_db_factory, repetitions=1))[0]
    driver = InteractiveScenarioDriver(
        FailingScenarios(),
        context_factory=lambda run: _resolved(owned.context),
        environment_factory=lambda ctx, item: SupportFixtureEnvironment(
            ae6b_db_factory, workspace_id=ctx.workspace_id
        ),
    )
    with pytest.raises(RuntimeError, match="injected preparation failure"):
        await driver.prepare(
            run=owned.run, variant=owned.variant, item=owned.item, case_identity=owned.identity
        )
    async with ae6b_db_factory() as session:
        remaining = await session.scalar(
            select(Customer).where(
                Customer.workspace_id == owned.identity.workspace_id,
                Customer.customer_ref.like("AE6B-%"),
            )
        )
        episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert episode is not None
        assert episode.cleanup_status == "TEARDOWN_COMPLETE"
        assert episode.termination_reason == "INTERACTIVE_PREPARATION_FAILED"
    assert remaining is None


async def _resolved(value):
    return value


class _ManifestSession:
    def __init__(self, manifest: object) -> None:
        self.manifest = manifest

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return None

    async def scalar(self, _statement):
        return self.manifest


class _ManifestSessionFactory:
    def __init__(self, manifest: object) -> None:
        self.manifest = manifest

    def __call__(self) -> _ManifestSession:
        return _ManifestSession(self.manifest)


@pytest.mark.asyncio
async def test_prepare_rejects_historical_manifest_before_environment_allocation() -> None:
    workspace_id, run_id, variant_id, item_id, case_id = (uuid4() for _ in range(5))
    identity = CaseExecutionIdentity(
        workspace_id=workspace_id,
        run_id=run_id,
        case_id=case_id,
        variant_id=variant_id,
        item_id=item_id,
        repetition_index=0,
        owner="historical-protocol-test",
        generation=1,
    )
    run = SimpleNamespace(
        id=run_id,
        workspace_id=workspace_id,
        experiment_id=uuid4(),
    )
    variant = SimpleNamespace(id=variant_id)
    item = SimpleNamespace(
        id=item_id,
        category="INTERACTIVE",
        expected={"assertions": {"ticket_count": 0}},
        input={
            "environment_ref": "support_customer",
            "simulator": {"max_turns": 3},
        },
    )
    scenarios = SimpleNamespace(
        runtime=SimpleNamespace(
            session_factory=_ManifestSessionFactory(default_evaluator_manifest())
        )
    )
    environment_requested = False

    async def context_factory(_run):
        return SimpleNamespace(
            workspace_id=str(workspace_id),
            permissions={"evaluation_run"},
        )

    def environment_factory(*_args):
        nonlocal environment_requested
        environment_requested = True
        raise AssertionError("historical protocol rejection must precede environment allocation")

    driver = InteractiveScenarioDriver(
        scenarios,
        context_factory=context_factory,
        environment_factory=environment_factory,
    )

    with pytest.raises(AgentHubError) as raised:
        await driver.prepare(run=run, variant=variant, item=item, case_identity=identity)

    assert raised.value.code == "INTERACTIVE_PROTOCOL_NOT_FROZEN"
    assert raised.value.status_code == 409
    assert environment_requested is False


def test_scripted_simulator_discloses_only_on_trigger() -> None:
    simulator = ScriptedUserSimulator(
        opening_text="我想退货，但不记得编号。",
        private_facts={"customer_ref": "C-AE6B-1"},
        triggers={"customer_ref": ["客户编号", "编号"]},
        max_turns=3,
    )

    import asyncio

    async def run():
        assert "C-AE6B-1" not in await simulator.opening()
        answer = await simulator.respond(
            transcript=[Turn("user", "我想退货")], agent_message="请提供客户编号"
        )
        assert answer == "C-AE6B-1"
        assert simulator.goal()["facts"] == ["customer_ref"]
        assert "customer_ref" in simulator.disclosure_policy()
        assert simulator.exhausted() is False

    asyncio.run(run())


def test_interactive_evaluator_requires_environment_assertion() -> None:
    metrics = evaluate_interactive({}, {"category": "INTERACTIVE"})
    assert metrics["task_success"].status == MetricStatus.NOT_AVAILABLE
    assert metrics["interactive_task_success"].status == MetricStatus.NOT_AVAILABLE


def test_interactive_evaluator_scores_state_and_teardown() -> None:
    observation = {
        "category": "INTERACTIVE",
        "executed_turn_count": 2,
        "execution_complete": True,
        "environment_lifecycle_status": "TEARDOWN_COMPLETE",
        "turns": [{"status": "SUCCEEDED"}, {"status": "SUCCEEDED"}],
        "stop_reason": "goal_met",
        "disclosure_policy_respected": True,
        "environment": {"assertion": {"passed": True, "observed": {}, "reason": None}},
    }
    metrics = evaluate_interactive({}, observation)
    assert metrics["task_success"].value == 1
    assert metrics["environment_state_success"].value == 1
    assert metrics["agent_execution_success"].value == 1
    assert metrics["turn_count"].value == 2


def test_interactive_agent_failure_cannot_be_success_even_if_state_passes() -> None:
    observation = {
        "category": "INTERACTIVE",
        "executed_turn_count": 1,
        "execution_complete": True,
        "environment_lifecycle_status": "TEARDOWN_COMPLETE",
        "turns": [{"status": "NEEDS_ATTENTION"}],
        "stop_reason": "agent_needs_attention",
        "disclosure_policy_respected": True,
        "environment": {"assertion": {"passed": True, "observed": {}, "reason": None}},
    }
    metrics = evaluate_interactive({}, observation)
    assert metrics["environment_state_success"].value == 1
    assert metrics["agent_execution_success"].value == 0
    assert metrics["task_success"].value == 0
    assert metrics["interactive_task_success"].value == 0


def test_interactive_budget_exhaustion_cannot_be_success() -> None:
    observation = {
        "category": "INTERACTIVE",
        "executed_turn_count": 1,
        "execution_complete": True,
        "environment_lifecycle_status": "TEARDOWN_COMPLETE",
        "turns": [{"status": "SUCCEEDED"}],
        "stop_reason": "budget_exhausted",
        "disclosure_policy_respected": True,
        "environment": {"assertion": {"passed": True, "observed": {}, "reason": None}},
    }
    metrics = evaluate_interactive({}, observation)
    assert metrics["task_success"].value == 0
    assert metrics["budget_exhausted"].value == 1


def test_interactive_without_turns_is_not_available_not_success() -> None:
    observation = {
        "category": "INTERACTIVE",
        "executed_turn_count": 0,
        "execution_complete": True,
        "environment_lifecycle_status": "TEARDOWN_COMPLETE",
        "turns": [],
        "stop_reason": "goal_met",
        "disclosure_policy_respected": True,
        "environment": {"assertion": {"passed": True, "observed": {}, "reason": None}},
    }
    metrics = evaluate_interactive({}, observation)
    assert metrics["task_success"].status == MetricStatus.NOT_AVAILABLE
    assert metrics["agent_execution_success"].status == MetricStatus.NOT_AVAILABLE


def test_interactive_canary_leak_cannot_be_success() -> None:
    observation = {
        "category": "INTERACTIVE",
        "executed_turn_count": 1,
        "execution_complete": True,
        "environment_lifecycle_status": "TEARDOWN_COMPLETE",
        "turns": [{"status": "SUCCEEDED"}],
        "stop_reason": "hidden_label_leak",
        "disclosure_policy_respected": False,
        "canary_leaked": True,
        "environment": {"assertion": {"passed": True, "observed": {}, "reason": None}},
    }
    metrics = evaluate_interactive({}, observation)
    assert metrics["disclosure_policy_respected"].value == 0
    assert metrics["task_success"].value == 0


def test_interactive_dataset_item_validates() -> None:
    item = {
        "case_key": "interactive-1",
        "split": "DEV",
        "category": "INTERACTIVE",
        "input": {
            "task": "我想办理退货，但我不记得客户编号。",
            "environment_ref": "support_customer",
            "simulator": {
                "private_facts": {"customer_ref": "__CUSTOMER_REF__"},
                "triggers": {"customer_ref": ["客户编号", "编号"]},
                "max_turns": 3,
            },
        },
        "expected": {
            "assertions": {"ticket_count": 1, "min_turns": 2},
            "canary": "AE6B-CANARY",
        },
        "tags": ["ae6b"],
        "source_provenance": {
            "source_kind": "synthetic_fixture",
            "source_id": "AE-6b:interactive-1",
        },
        "ordinal": 0,
    }
    validated = validate_dataset_item(item)
    assert validated["category"] == "INTERACTIVE"
    assert validated["input"]["task"] == item["input"]["task"]
