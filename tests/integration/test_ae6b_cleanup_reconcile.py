from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from packages.agent_runtime.models import AgentRun
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.interactive_observation import observation_envelope
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    InteractiveEpisode,
)
from packages.evaluation.runner import (
    CaseExecutionObservation,
    ExperimentRunner,
    reconcile_experiment_runs,
)
from packages.threads.service import ThreadService
from packages.tools.models import Customer
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import (
    create_bound_episode_turn,
    create_interactive_driver_experiment,
    create_owned_lifecycle_cases,
    fail_customer_mutation,
    reserve_episode,
)

__all__ = ["db_factory", "migrated_database"]
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_retained_evaluation_thread_deletion_has_a_documented_conflict(db_factory):
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    fixture = await lifecycle.create_fixture(owned.identity)
    bound = await create_bound_episode_turn(
        db_factory, owned, episode, opening="retained evaluation conversation"
    )
    with pytest.raises(AgentHubError) as rejected:
        await ThreadService(db_factory).delete_thread(owned.context, bound.thread.id)
    assert rejected.value.code == "EVALUATION_EVIDENCE_RETAINED"
    assert rejected.value.status_code == 409
    async with db_factory() as session:
        assert await session.get(Customer, fixture.customer_id) is not None
    await lifecycle.terminate(owned.identity, reason="retained_thread_test_cleanup")
    assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("retain_false_assertion", [False, True])
async def test_observation_cannot_precede_or_contradict_retained_assertion(
    db_factory, retain_false_assertion
):
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    fixture = await lifecycle.create_fixture(owned.identity)
    if retain_false_assertion:
        await lifecycle.retain_assertion(
            owned.identity,
            assertion={
                "passed": False,
                "reason": "ticket_count",
                "observed": {"ticket_count": 0, "ticket_statuses": []},
            },
            source="controlled_assertion_test",
        )
    envelope = observation_envelope(
        CaseExecutionObservation(
            observation={
                "category": "INTERACTIVE",
                "stop_reason": "goal_met",
                "environment": {
                    "assertion": {"passed": True, "reason": None, "observed": {"ticket_count": 1}}
                },
            }
        )
    )
    with pytest.raises(AgentHubError) as rejected:
        await lifecycle.retain_observation(owned.identity, envelope=envelope)
    assert rejected.value.code == (
        "INTERACTIVE_ASSERTION_EVIDENCE_MISMATCH"
        if retain_false_assertion
        else "INTERACTIVE_ASSERTION_EVIDENCE_MISSING"
    )
    async with db_factory() as session:
        retained = await session.get(InteractiveEpisode, owned.case.id)
        assert retained.observation_envelope is None
        assert retained.observation_hash is None
        assert await session.get(Customer, fixture.customer_id) is not None
    await lifecycle.terminate(owned.identity, reason="controlled_assertion_test_cleanup")
    assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
async def test_case_failure_log_omits_exception_business_content(db_factory, caplog):
    setup = await create_interactive_driver_experiment(db_factory, assertions={"ticket_count": 0})
    private_text = "PRIVATE-CONTROLLED-CUSTOMER-CONTENT-NEVER-LOG"

    class RejectedDriver:
        async def prepare(self, **kwargs):
            raise RuntimeError(private_text)

    await ExperimentRunner(db_factory, driver=RejectedDriver()).execute(
        run_id=setup.run.id, owner="controlled-log-check", settings=Settings(testing=True)
    )
    assert any(record.message == "evaluation_case_failed" for record in caplog.records)
    assert private_text not in caplog.text
    async with db_factory() as session:
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.experiment_run_id == setup.run.id
            )
        )
        assert case.status == "FAILED"
        assert case.failure_code == "EVALUATION_CASE_FAILED"


@pytest.mark.asyncio
async def test_cleanup_retry_is_discovered_after_case_and_parent_are_terminal(db_factory):
    owned, adjacent = await create_owned_lifecycle_cases(
        db_factory, workspace_count=2, repetitions=1
    )
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    await reserve_episode(lifecycle, adjacent)
    episode = await lifecycle.create_fixture(owned.identity)
    other_episode = await lifecycle.create_fixture(adjacent.identity)
    await lifecycle.terminate(owned.identity, reason="controlled_preparation_failure")
    async with fail_customer_mutation(
        db_factory, workspace_id=owned.identity.workspace_id, operation="delete"
    ):
        assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_FAILED"

    async with db_factory() as session:
        case = await session.get(EvaluationExperimentCaseResult, owned.case.id)
        parent = await session.get(EvaluationExperimentRun, owned.run.id)
        retained = await session.get(InteractiveEpisode, owned.case.id)
        case.status = "FAILED"
        case.failure_code = "CONTROLLED_PREPARATION_FAILURE"
        parent.status = "FAILED"
        parent.lease_owner = None
        parent.lease_expires_at = None
        retained.cleanup_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        # The injected deletion error rolls back its snapshot transaction too;
        # the original fixture remains available for the successful retry.
        assert retained.assertion_hash is None
        await session.commit()

    assert await InteractiveLifecycle(db_factory).reconcile_cleanup(limit=1000) >= 1
    async with db_factory() as session:
        retained = await session.get(InteractiveEpisode, owned.case.id)
        case = await session.get(EvaluationExperimentCaseResult, owned.case.id)
        assert retained.cleanup_status == "TEARDOWN_COMPLETE"
        assert retained.execution_status == "TERMINATED"
        assert retained.assertion == {
            "passed": False,
            "reason": "interrupted_before_task_assertion",
            "observed": {"ticket_count": 0, "ticket_statuses": []},
        }
        assert retained.assertion_hash == canonical_json_hash(retained.assertion)
        assert retained.assertion_source == "recovery_fixture_snapshot"
        assert retained.observation_envelope is None
        assert case.status == "FAILED"
        assert case.failure_code == "CONTROLLED_PREPARATION_FAILURE"
        assert await session.get(Customer, episode.customer_id) is None
        assert await session.get(Customer, other_episode.customer_id) is not None
    await lifecycle.terminate(adjacent.identity, reason="adjacent_test_cleanup")
    assert await lifecycle.cleanup(adjacent.identity) == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
async def test_cancel_reconcile_does_not_treat_first_terminal_run_as_a_complete_episode(db_factory):
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    fixture = await lifecycle.create_fixture(owned.identity)
    bound = await create_bound_episode_turn(
        db_factory, owned, episode, opening="controlled first turn"
    )
    await lifecycle.dispatch(owned.identity, turn_index=0)
    # This is a storage-boundary injection, not a claim of a provider execution.
    async with db_factory() as session:
        run = await session.get(AgentRun, bound.run.id)
        run.status = "SUCCEEDED"
        await session.commit()
    await lifecycle.record_turn(
        owned.identity,
        turn_index=0,
        evidence={"run_id": str(bound.run.id), "status": "SUCCEEDED"},
    )
    await lifecycle.reserve_turn(
        owned.identity, turn_index=1, input_hash=canonical_json_hash("not yet dispatched")
    )
    async with db_factory() as session:
        parent = await session.get(EvaluationExperimentRun, owned.run.id)
        parent.status = "CANCEL_REQUESTED"
        parent.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class RecordingQueue:
        def __init__(self):
            self.calls = []

        async def enqueue(self, run_id):
            self.calls.append(run_id)

    queue = RecordingQueue()
    await reconcile_experiment_runs(
        db_factory,
        queue=queue,
        settings=Settings(
            testing=True, evaluation_runner_lease_seconds=5, evaluation_runner_heartbeat_seconds=1
        ),
    )
    assert owned.run.id not in queue.calls
    async with db_factory() as session:
        parent = await session.get(EvaluationExperimentRun, owned.run.id)
        case = await session.get(EvaluationExperimentCaseResult, owned.case.id)
        retained = await session.get(InteractiveEpisode, owned.case.id)
        first = await session.get(AgentRun, bound.run.id)
        assert parent.status == "CANCELLED"
        assert case.status == "CANCELLED"
        assert case.failure_code == "EVALUATION_INTERACTIVE_CANCELLED"
        assert case.observation["execution_complete"] is False
        assert first.status == "SUCCEEDED"
        assert retained.observation_envelope is None
        assert retained.assertion["passed"] is False
        assert retained.cleanup_status == "TEARDOWN_COMPLETE"
        assert await session.get(Customer, fixture.customer_id) is None
