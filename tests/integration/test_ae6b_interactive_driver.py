"""PostgreSQL integration coverage for the deterministic interactive driver path.

These tests invoke ExperimentRunner directly. ``ScriptedGateway`` replaces only the
provider boundary; this module is not a Celery queue end-to-end test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.environment import SupportFixtureEnvironment
from packages.evaluation.interactive_driver import InteractiveScenarioDriver
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.evaluation.runner import ExperimentRunner
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.model_gateway.contracts import ModelResponse, ModelUsage
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import ScriptedGateway
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import (
    InteractiveExperimentSetup,
    create_interactive_driver_experiment,
)

__all__ = ["db_factory", "migrated_database"]

pytestmark = pytest.mark.integration

_ASSERTIONS = {
    "min_turns": 2,
    "ticket_count": 0,
}


class _FailSecondTurnPreparation(AgentRunService):
    """Keep the real run service; fail only the second durable turn preparation."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.prepare_attempts = 0
        self.failed_prepare_input: str | None = None

    async def prepare_run(self, context, **kwargs):
        self.prepare_attempts += 1
        if self.prepare_attempts == 2:
            self.failed_prepare_input = kwargs["input_text"]
            raise RuntimeError("INJECTED_SECOND_TURN_PREPARATION_FAILURE")
        return await super().prepare_run(context, **kwargs)


def _interactive_driver(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    setup: InteractiveExperimentSetup,
    runtime: AgentRunService,
) -> InteractiveScenarioDriver:
    async def context_factory(_run) -> WorkspaceExecutionContext:
        return setup.context

    def environment_factory(context, _item) -> SupportFixtureEnvironment:
        return SupportFixtureEnvironment(
            session_factory,
            workspace_id=context.workspace_id,
        )

    return InteractiveScenarioDriver(
        ScriptedScenarioDriver(runtime, driver_kind="controlled"),
        context_factory=context_factory,
        environment_factory=environment_factory,
    )


@pytest.mark.asyncio
async def test_two_turn_disclosure_runs_through_real_runner_and_retains_evidence(
    db_factory,
) -> None:
    setup = await create_interactive_driver_experiment(
        db_factory,
        assertions=_ASSERTIONS,
    )
    assert setup.item.expected["assertions"] == _ASSERTIONS
    assert "max_cost_cny" not in setup.item.input

    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="Please provide your customer reference.",
                provider="fake",
                model="fake",
                usage=ModelUsage(
                    input_tokens=3,
                    output_tokens=2,
                    total_tokens=5,
                    cached_tokens=0,
                ),
            ),
            ModelResponse(
                content="The support request is complete.",
                provider="fake",
                model="fake",
                usage=ModelUsage(
                    input_tokens=4,
                    output_tokens=1,
                    total_tokens=5,
                    cached_tokens=0,
                ),
            ),
        ]
    )
    # This replacement stops at the provider boundary; threads, turns, runs,
    # lifecycle persistence, ExperimentRunner claims, and cleanup remain real.
    runtime = AgentRunService(
        db_factory,
        model_gateway_factory=lambda _session: gateway,
    )
    runner = ExperimentRunner(
        db_factory,
        driver=_interactive_driver(db_factory, setup=setup, runtime=runtime),
    )

    await runner.execute(
        run_id=setup.run.id,
        owner="ae6b-interactive-driver-success",
        settings=Settings(testing=True),
    )

    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, setup.run.id)
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.experiment_run_id == setup.run.id
            )
        )
        assert run is not None and case is not None
        episode = await session.get(InteractiveEpisode, case.id)
        turns = list(
            await session.scalars(
                select(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == case.workspace_id,
                    InteractiveEpisodeTurn.case_id == case.id,
                )
                .order_by(InteractiveEpisodeTurn.turn_index)
            )
        )
        persisted_runs = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == case.workspace_id,
                    AgentRun.id.in_(
                        [turn.agent_run_id for turn in turns if turn.agent_run_id is not None]
                    ),
                )
            )
        )
        item = await session.get(EvaluationDatasetItem, setup.item.id)

        assert run.status == "SUCCEEDED"
        assert case.status == "SUCCEEDED"
        assert case.agent_run_id is not None
        assert case.input_tokens == 7
        assert case.output_tokens == 3
        assert case.total_tokens == 10
        assert case.cached_tokens == 0
        assert case.cost_amount is None
        assert case.observation["executed_turn_count"] == 2
        assert case.observation["stop_reason"] == "goal_met"
        assert case.observation["agent_execution_complete"] is True
        assert case.observation["environment_lifecycle_status"] == "TEARDOWN_COMPLETE"
        assert case.observation["aggregate_billing_complete"] is False
        assert case.observation["environment"]["assertion"]["passed"] is True
        assert case.observation["environment"]["assertion"]["observed"]["ticket_count"] == 0

        assert item is not None
        assert item.input == setup.item.input
        assert item.expected["assertions"] == {"min_turns": 2, "ticket_count": 0}
        assert episode is not None
        assert episode.execution_status == "COMPLETED"
        assert episode.cleanup_status == "TEARDOWN_COMPLETE"
        assert episode.assertion is not None
        assert episode.assertion["passed"] is True
        assert episode.assertion["observed"]["ticket_count"] == 0
        assert episode.assertion_hash == canonical_json_hash(episode.assertion)
        assert episode.assertion_source == "support_fixture.assert_state"
        assert episode.observation_envelope is not None
        assert episode.observation_hash == canonical_json_hash(episode.observation_envelope)

        assert len(turns) == 2
        assert [turn.turn_index for turn in turns] == [0, 1]
        assert all(turn.thread_turn_id is not None for turn in turns)
        assert all(turn.agent_run_id is not None for turn in turns)
        assert all(turn.dispatch_status == "TERMINAL" for turn in turns)
        assert [turn.client_token for turn in turns] == [
            f"{case.id.hex}:0",
            f"{case.id.hex}:1",
        ]
        first_input = str(setup.item.input["task"])
        assert episode.customer_ref is not None
        assert [turn.input_hash for turn in turns] == [
            canonical_json_hash(first_input),
            canonical_json_hash(episode.customer_ref),
        ]
        assert turns[0].input_hash != turns[1].input_hash
        for turn in turns:
            assert turn.evidence is not None
            assert turn.evidence["user_input_hash"] == turn.input_hash

        assert len(persisted_runs) == 2
        runs_by_id = {agent_run.id: agent_run for agent_run in persisted_runs}
        assert all(runs_by_id[turn.agent_run_id].status == "SUCCEEDED" for turn in turns)
        assert runs_by_id[turns[0].agent_run_id].input_text == first_input
        assert runs_by_id[turns[1].agent_run_id].input_text == episode.customer_ref
        assert case.agent_run_id == turns[0].agent_run_id

        customer_count = await session.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.workspace_id == case.workspace_id)
        )
        ticket_count = await session.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.workspace_id == case.workspace_id)
        )
        assert customer_count == 0
        assert ticket_count == 0

        metrics_service = EvaluationMetricsService()
        metrics = await metrics_service.materialize_metrics(
            session,
            context=setup.context,
            run_id=setup.run.id,
        )
        persisted_metrics = await metrics_service.get_persisted_metrics(
            session,
            context=setup.context,
            run_id=setup.run.id,
        )
        variant_metrics = metrics["variants"][str(setup.variant.id)]
        assert variant_metrics["task_success"]["value"] == 1
        assert variant_metrics["task_success"]["sample_count"] == 1
        assert persisted_metrics["metrics_hash"] == metrics["metrics_hash"]

    assert len(gateway.requests) == 2


@pytest.mark.asyncio
async def test_second_turn_preparation_failure_never_promotes_first_success_or_replays(
    db_factory,
) -> None:
    setup = await create_interactive_driver_experiment(
        db_factory,
        assertions=_ASSERTIONS,
    )
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="Please provide your customer reference.",
                provider="fake",
                model="fake",
                usage=ModelUsage(
                    input_tokens=3,
                    output_tokens=2,
                    total_tokens=5,
                    cached_tokens=0,
                ),
            )
        ]
    )
    runtime = _FailSecondTurnPreparation(
        db_factory,
        model_gateway_factory=lambda _session: gateway,
    )
    runner = ExperimentRunner(
        db_factory,
        driver=_interactive_driver(db_factory, setup=setup, runtime=runtime),
    )

    await runner.execute(
        run_id=setup.run.id,
        owner="ae6b-interactive-driver-prepare-failure",
        settings=Settings(testing=True),
    )

    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, setup.run.id)
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.experiment_run_id == setup.run.id
            )
        )
        assert run is not None and case is not None
        episode = await session.get(InteractiveEpisode, case.id)
        turns = list(
            await session.scalars(
                select(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == case.workspace_id,
                    InteractiveEpisodeTurn.case_id == case.id,
                )
                .order_by(InteractiveEpisodeTurn.turn_index)
            )
        )

        assert runtime.prepare_attempts == 2
        assert episode is not None
        assert runtime.failed_prepare_input == episode.customer_ref
        assert run.status == "FAILED"
        assert case.status == "FAILED"
        assert case.failure_code == "EVALUATION_CASE_FAILED"
        assert case.agent_run_id is not None
        assert episode.execution_status == "RUNNING"
        assert episode.assertion is None
        assert episode.assertion_hash is None
        assert episode.observation_envelope is None
        assert episode.observation_hash is None
        assert episode.cleanup_status == "PENDING"

        assert len(turns) == 2
        first, interrupted = turns
        assert first.turn_index == 0
        assert first.dispatch_status == "TERMINAL"
        assert first.thread_turn_id is not None
        assert first.agent_run_id == case.agent_run_id
        assert first.evidence is not None
        assert first.evidence["status"] == "SUCCEEDED"
        assert first.evidence["run_id"] == str(first.agent_run_id)
        assert first.evidence["user_input_hash"] == first.input_hash

        first_run = await session.get(AgentRun, first.agent_run_id)
        assert first_run is not None
        assert first_run.status == "SUCCEEDED"
        assert first_run.final_output == "Please provide your customer reference."

        assert interrupted.turn_index == 1
        assert interrupted.dispatch_status == "RESERVED"
        assert interrupted.thread_turn_id is None
        assert interrupted.agent_run_id is None
        assert interrupted.input_hash == canonical_json_hash(episode.customer_ref)

        assert (
            await session.scalar(
                select(func.count())
                .select_from(Customer)
                .where(
                    Customer.workspace_id == case.workspace_id,
                    Customer.id == episode.customer_id,
                )
            )
            == 1
        )

    # A second runner invocation finds a terminal run and cannot turn the first
    # successful turn into a full task success or replay the provider.
    await runner.execute(
        run_id=setup.run.id,
        owner="ae6b-interactive-driver-prepare-failure-retry",
        settings=Settings(testing=True),
    )
    assert len(gateway.requests) == 1
