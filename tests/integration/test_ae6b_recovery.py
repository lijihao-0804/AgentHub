"""Real-PostgreSQL recovery windows for the interactive lifecycle.

Each test persists a database state that represents a process stop at a known
boundary, then lets a new runner instance recover it.  No model or worker process
is killed or replayed here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.interactive_observation import observation_envelope
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.evaluation.runner import CaseExecutionObservation, ExperimentRunner
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.tools.models import Customer, Ticket
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import (
    create_bound_episode_turn,
    create_owned_lifecycle_cases,
    reserve_episode,
)

__all__ = ["db_factory", "migrated_database"]

pytestmark = pytest.mark.integration

_RECOVERY_REQUIRED = "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"


class ForbiddenDriver:
    """Fail immediately if recovery tries to execute model work again."""

    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, session, *, run, variant, item) -> CaseExecutionObservation:
        del session, run, variant, item
        self.execute_calls += 1
        raise AssertionError("RECOVERY_MUST_NOT_DISPATCH_A_MODEL")


async def _recover_with_new_runner(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
) -> ForbiddenDriver:
    """Expire the old lease, claim as a new owner, and recover without execution."""

    async with session_factory() as session:
        parent_run = await session.get(EvaluationExperimentRun, run_id)
        assert parent_run is not None
        parent_run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    driver = ForbiddenDriver()
    runner = ExperimentRunner(session_factory, driver=driver)
    new_owner = f"ae6b-recovery-{uuid4().hex}"
    async with session_factory() as session:
        claimed = await runner.claim_run(
            session,
            run_id=run_id,
            owner=new_owner,
            lease_seconds=1_800,
        )
        assert claimed is True

    async with session_factory() as session:
        await runner.recover_inflight_cases(session, run_id=run_id)

    assert driver.execute_calls == 0
    return driver


async def _mark_run_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: UUID,
    run_id: UUID,
    final_output: str,
) -> None:
    """Create a known lower-level failure state; this is not an agent E2E result."""

    async with session_factory() as session:
        agent_run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.id == run_id,
            )
        )
        assert agent_run is not None
        agent_run.status = "SUCCEEDED"
        agent_run.execution_owner = None
        agent_run.execution_lease_expires_at = None
        agent_run.final_output = final_output
        agent_run.failure_code = None
        agent_run.completed_at = datetime.now(UTC)
        await session.commit()


@pytest.mark.asyncio
async def test_fixture_committed_before_first_turn_is_cleaned_without_replay(
    db_factory,
) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    customer_id = episode.customer_id
    assert customer_id is not None

    driver = await _recover_with_new_runner(db_factory, run_id=owned.run.id)

    async with db_factory() as session:
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.workspace_id == owned.identity.workspace_id,
                EvaluationExperimentCaseResult.id == owned.identity.case_id,
            )
        )
        recovered_episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        episode_count = await session.scalar(
            select(func.count())
            .select_from(InteractiveEpisode)
            .where(
                InteractiveEpisode.workspace_id == owned.identity.workspace_id,
                InteractiveEpisode.case_id == owned.identity.case_id,
            )
        )
        customer_count = await session.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.workspace_id == owned.identity.workspace_id)
        )
        agent_run_count = await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.workspace_id == owned.identity.workspace_id,
                AgentRun.thread_id == episode.reserved_thread_id,
            )
        )

        assert case is not None
        assert recovered_episode is not None
        assert case.status == "FAILED"
        assert case.failure_code == _RECOVERY_REQUIRED
        assert recovered_episode.cleanup_status == "TEARDOWN_COMPLETE"
        assert recovered_episode.customer_id == customer_id
        assert recovered_episode.assertion_source == "recovery_fixture_snapshot"
        assert recovered_episode.assertion is not None
        assert recovered_episode.assertion["passed"] is False
        assert recovered_episode.assertion["observed"]["ticket_count"] == 0
        assert await session.get(Customer, customer_id) is None
        assert episode_count == 1
        assert customer_count == 0
        assert agent_run_count == 0

    assert driver.execute_calls == 0


@pytest.mark.asyncio
async def test_prepared_but_unbound_first_turn_is_recovered_and_cancelled(
    db_factory,
) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    customer_id = episode.customer_id
    assert customer_id is not None

    opening = "Explain how I can recover the interrupted support request."
    await lifecycle.reserve_turn(
        owned.identity,
        turn_index=0,
        input_hash=sha256(opening.encode("utf-8")).hexdigest(),
    )
    prepared = await ScriptedScenarioDriver(
        AgentRunService(db_factory),
        driver_kind="controlled",
    ).prepare_first(
        owned.context,
        agent_version_id=owned.variant.agent_version_id,
        first_input=opening,
        knowledge_snapshots=owned.variant.effective_knowledge_snapshots,
        wiki_publications=owned.variant.effective_wiki_publications,
        instance_id=owned.identity.case_id.hex,
        thread_id=episode.reserved_thread_id,
    )
    assert prepared.instance_id == owned.identity.case_id.hex
    assert prepared.thread_id == episode.reserved_thread_id

    driver = await _recover_with_new_runner(db_factory, run_id=owned.run.id)

    async with db_factory() as session:
        case = await session.get(EvaluationExperimentCaseResult, owned.identity.case_id)
        recovered_episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        recovered_turn = await session.scalar(
            select(InteractiveEpisodeTurn).where(
                InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                InteractiveEpisodeTurn.turn_index == 0,
            )
        )
        recovered_run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == owned.identity.workspace_id,
                AgentRun.id == prepared.run_id,
            )
        )
        thread_run_count = await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.workspace_id == owned.identity.workspace_id,
                AgentRun.thread_id == episode.reserved_thread_id,
            )
        )
        episode_count = await session.scalar(
            select(func.count())
            .select_from(InteractiveEpisode)
            .where(
                InteractiveEpisode.workspace_id == owned.identity.workspace_id,
                InteractiveEpisode.case_id == owned.identity.case_id,
            )
        )
        customer_count = await session.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.workspace_id == owned.identity.workspace_id)
        )

        assert case is not None
        assert recovered_episode is not None
        assert recovered_turn is not None
        assert recovered_run is not None
        assert case.status == "FAILED"
        assert case.failure_code == _RECOVERY_REQUIRED
        assert case.agent_run_id == prepared.run_id
        assert recovered_turn.thread_turn_id == prepared.turn_id
        assert recovered_turn.agent_run_id == prepared.run_id
        assert recovered_run.status == "CANCELLED"
        assert recovered_episode.thread_id == prepared.thread_id
        assert recovered_episode.cleanup_status == "TEARDOWN_COMPLETE"
        assert await session.get(Customer, customer_id) is None
        assert thread_run_count == 1
        assert episode_count == 1
        assert customer_count == 0

    assert driver.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_turn_before_crash",
    [False, True],
    ids=["terminal-run-before-record-turn", "after-record-turn"],
)
async def test_terminal_first_run_cannot_close_partial_conversation(
    db_factory,
    record_turn_before_crash: bool,
) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    customer_id = episode.customer_id
    assert customer_id is not None
    opening = "The first turn was executed before the worker stopped."
    bound = await create_bound_episode_turn(
        db_factory,
        owned,
        episode,
        opening=opening,
    )
    await lifecycle.dispatch(owned.identity, turn_index=0)
    await _mark_run_succeeded(
        db_factory,
        workspace_id=owned.identity.workspace_id,
        run_id=bound.run.id,
        final_output="First turn is complete; the conversation is not.",
    )

    async with db_factory() as session:
        session.add(
            Ticket(
                workspace_id=owned.identity.workspace_id,
                customer_id=customer_id,
                ticket_ref=f"T-RECOVERY-{uuid4().hex}",
                subject="Interrupted support request",
                status="OPEN",
            )
        )
        await session.commit()

    recorded_evidence: dict[str, object] | None = None
    recorded_evidence_hash: str | None = None
    if record_turn_before_crash:
        evidence = {
            "run_id": str(bound.run.id),
            "thread_turn_id": str(bound.turn.id),
            "observed_agent_status": "SUCCEEDED",
            "failure_code": None,
            "business_assertion": {
                "passed": False,
                "reason": "conversation_not_complete",
                "observed": {
                    "ticket_count": 1,
                    "ticket_statuses": ["OPEN"],
                },
                "source": "support_fixture.assert_state.partial",
            },
        }
        await lifecycle.record_turn(
            owned.identity,
            turn_index=0,
            evidence=evidence,
        )
        async with db_factory() as session:
            stored_turn = await session.scalar(
                select(InteractiveEpisodeTurn).where(
                    InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                    InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                    InteractiveEpisodeTurn.turn_index == 0,
                )
            )
            assert stored_turn is not None
            assert stored_turn.evidence is not None
            recorded_evidence = stored_turn.evidence
            recorded_evidence_hash = stored_turn.evidence_hash
            assert recorded_evidence_hash == canonical_json_hash(recorded_evidence)
    else:
        async with db_factory() as session:
            stored_turn = await session.scalar(
                select(InteractiveEpisodeTurn).where(
                    InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                    InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                    InteractiveEpisodeTurn.turn_index == 0,
                )
            )
            assert stored_turn is not None
            assert stored_turn.evidence is None
            assert stored_turn.evidence_hash is None

    await lifecycle.reserve_turn(
        owned.identity,
        turn_index=1,
        input_hash=canonical_json_hash("second turn deliberately not dispatched"),
    )

    driver = await _recover_with_new_runner(db_factory, run_id=owned.run.id)

    async with db_factory() as session:
        case = await session.get(EvaluationExperimentCaseResult, owned.identity.case_id)
        recovered_episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        turns = list(
            await session.scalars(
                select(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                    InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                )
                .order_by(InteractiveEpisodeTurn.turn_index)
            )
        )
        runs = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == owned.identity.workspace_id,
                    AgentRun.thread_id == episode.reserved_thread_id,
                )
            )
        )
        customer_count = await session.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.workspace_id == owned.identity.workspace_id)
        )
        ticket_count = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.workspace_id == owned.identity.workspace_id)
        )
        episode_count = await session.scalar(
            select(func.count())
            .select_from(InteractiveEpisode)
            .where(
                InteractiveEpisode.workspace_id == owned.identity.workspace_id,
                InteractiveEpisode.case_id == owned.identity.case_id,
            )
        )

        assert case is not None
        assert recovered_episode is not None
        assert case.status == "FAILED"
        assert case.failure_code == _RECOVERY_REQUIRED
        assert case.agent_run_id == bound.run.id
        assert recovered_episode.execution_status == "RECOVERY_REQUIRED"
        assert recovered_episode.termination_reason == _RECOVERY_REQUIRED
        assert recovered_episode.cleanup_status == "TEARDOWN_COMPLETE"
        assert recovered_episode.assertion_source == "recovery_fixture_snapshot"
        assert recovered_episode.assertion is not None
        assert recovered_episode.assertion == {
            "passed": False,
            "reason": "interrupted_before_task_assertion",
            "observed": {
                "ticket_count": 1,
                "ticket_statuses": ["OPEN"],
            },
        }
        assert recovered_episode.assertion_hash == canonical_json_hash(recovered_episode.assertion)
        assert len(turns) == 2
        assert turns[0].thread_turn_id == bound.turn.id
        assert turns[0].agent_run_id == bound.run.id
        assert turns[0].dispatch_status == "TERMINAL"
        assert turns[0].terminal_status == "SUCCEEDED"
        assert turns[0].evidence is not None
        assert turns[0].evidence_hash is not None
        assert turns[0].evidence_hash == canonical_json_hash(turns[0].evidence)
        if record_turn_before_crash:
            assert recorded_evidence is not None
            assert recorded_evidence_hash is not None
            assert turns[0].evidence == recorded_evidence
            assert turns[0].evidence_hash == recorded_evidence_hash
        else:
            assert turns[0].evidence["evidence_source"] == "agent_run_terminal_storage"
            assert turns[0].evidence["run_id"] == str(bound.run.id)
            assert turns[0].evidence["status"] == "SUCCEEDED"
        assert turns[1].dispatch_status == "RESERVED"
        assert turns[1].agent_run_id is None
        assert len(runs) == 1
        assert runs[0].id == bound.run.id
        assert runs[0].status == "SUCCEEDED"
        if not record_turn_before_crash:
            assert turns[0].evidence["run_id"] == str(runs[0].id)
            assert turns[0].evidence["status"] == runs[0].status
        assert await session.get(Customer, customer_id) is None
        assert customer_count == 0
        assert ticket_count == 0
        assert episode_count == 1

    assert driver.execute_calls == 0


@pytest.mark.asyncio
async def test_retained_observation_materializes_result_without_rerun(
    db_factory,
) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    customer_id = episode.customer_id
    assert customer_id is not None
    opening = "Complete one deterministic interactive turn."
    bound = await create_bound_episode_turn(
        db_factory,
        owned,
        episode,
        opening=opening,
    )
    await lifecycle.dispatch(owned.identity, turn_index=0)
    await _mark_run_succeeded(
        db_factory,
        workspace_id=owned.identity.workspace_id,
        run_id=bound.run.id,
        final_output="The scripted turn is complete.",
    )
    await lifecycle.record_turn(
        owned.identity,
        turn_index=0,
        evidence={
            "run_id": str(bound.run.id),
            "thread_turn_id": str(bound.turn.id),
            "observed_agent_status": "SUCCEEDED",
            "failure_code": None,
        },
    )

    assertion = {
        "passed": True,
        "reason": None,
        "observed": {
            "ticket_count": 0,
            "ticket_statuses": [],
        },
        "source": "support_fixture.assert_state",
    }
    await lifecycle.retain_assertion(
        owned.identity,
        assertion=assertion,
        source="support_fixture.assert_state",
    )
    envelope = observation_envelope(
        CaseExecutionObservation(
            observation={
                "category": "INTERACTIVE",
                "executed_turn_count": 1,
                "execution_complete": True,
                "agent_execution_complete": True,
                "stop_reason": "goal_met",
                "turns": [{"turn_index": 0, "status": "SUCCEEDED"}],
                "observed_agent_status": "SUCCEEDED",
                "observed_agent_failure_code": None,
                "environment_lifecycle_status": "PENDING",
                "environment": {
                    "kind": "support_fixture",
                    "version": "environment-v2",
                    "assertion": assertion,
                    "teardown_status": "PENDING",
                    "teardown_error": None,
                },
                "business_outcome": assertion,
            },
            agent_run_id=bound.run.id,
            latency_ms=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_tokens=None,
            cost_amount=None,
            cost_currency=None,
            aggregate_billing_complete=False,
        )
    )
    await lifecycle.retain_observation(owned.identity, envelope=envelope)

    async with db_factory() as session:
        stored_before = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert stored_before is not None
        assertion_hash = stored_before.assertion_hash
        observation_hash = stored_before.observation_hash
        assert assertion_hash is not None
        assert observation_hash is not None

    driver = await _recover_with_new_runner(db_factory, run_id=owned.run.id)

    async with db_factory() as session:
        case = await session.get(EvaluationExperimentCaseResult, owned.identity.case_id)
        episode_after = await session.get(InteractiveEpisode, owned.identity.case_id)
        thread_runs = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == owned.identity.workspace_id,
                    AgentRun.thread_id == episode.reserved_thread_id,
                )
            )
        )
        customer_count = await session.scalar(
            select(func.count())
            .select_from(Customer)
            .where(Customer.workspace_id == owned.identity.workspace_id)
        )
        episode_count = await session.scalar(
            select(func.count())
            .select_from(InteractiveEpisode)
            .where(
                InteractiveEpisode.workspace_id == owned.identity.workspace_id,
                InteractiveEpisode.case_id == owned.identity.case_id,
            )
        )

        assert case is not None
        assert episode_after is not None
        assert case.status == "SUCCEEDED"
        assert case.agent_run_id == bound.run.id
        assert case.latency_ms is None
        assert case.input_tokens is None
        assert case.output_tokens is None
        assert case.total_tokens is None
        assert case.cached_tokens is None
        assert case.cost_amount is None
        assert case.cost_currency is None
        assert case.observation["aggregate_billing_complete"] is False
        assert case.observation["executed_turn_count"] == 1
        assert case.observation["environment_lifecycle_status"] == "TEARDOWN_COMPLETE"
        assert episode_after.assertion_hash == assertion_hash
        assert episode_after.observation_hash == observation_hash
        assert episode_after.assertion == assertion
        assert episode_after.observation_envelope == envelope
        assert episode_after.cleanup_status == "TEARDOWN_COMPLETE"
        assert await session.get(Customer, customer_id) is None
        assert len(thread_runs) == 1
        assert thread_runs[0].id == bound.run.id
        assert customer_count == 0
        assert episode_count == 1

    assert driver.execute_calls == 0
