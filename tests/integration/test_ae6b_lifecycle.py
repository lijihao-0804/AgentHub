"""PostgreSQL acceptance tests for durable interactive execution lifecycle."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from packages.agent_runtime.models import AgentRun
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.environment import SupportFixtureEnvironment
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.evaluation.runner import ExperimentRunner
from packages.threads.service import ThreadService
from packages.tools.models import Customer, Ticket
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import (
    FIXTURE_REF,
    SEED,
    create_bound_episode_turn,
    create_owned_lifecycle_cases,
    fail_customer_mutation,
    reserve_episode,
)

__all__ = ["db_factory", "migrated_database"]

pytestmark = pytest.mark.integration


def _completed_observation(agent_run_id: UUID | None) -> dict[str, object]:
    return {
        "observation": {
            "category": "INTERACTIVE",
            "execution_complete": True,
            "turns": [{"status": "SUCCEEDED"}],
        },
        "agent_run_id": str(agent_run_id) if agent_run_id is not None else None,
        "latency_ms": 25,
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cached_tokens": 0,
        "cost_amount": None,
        "cost_currency": None,
        "aggregate_billing_complete": False,
        "failure_code": None,
        "safe_failure_message": None,
    }


@pytest.mark.asyncio
async def test_reservation_prepare_and_environment_interop_are_idempotent(db_factory) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)

    started = await reserve_episode(lifecycle, owned)
    repeated = await reserve_episode(lifecycle, owned)
    assert repeated.case_id == started.case_id == owned.identity.case_id
    assert repeated.instance_id == started.instance_id == owned.identity.case_id
    assert repeated.reserved_thread_id == started.reserved_thread_id
    assert repeated.driver_version == "interactive-driver-v2"
    assert repeated.env_version == "environment-v2"
    assert repeated.simulator_version == "scripted-disclosure-v1"

    fixture = await lifecycle.create_fixture(owned.identity)
    repeated_fixture = await lifecycle.create_fixture(owned.identity)
    assert repeated_fixture.customer_id == fixture.customer_id
    assert repeated_fixture.customer_ref == fixture.customer_ref
    async with db_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Customer)
                .where(
                    Customer.workspace_id == owned.identity.workspace_id,
                    Customer.id == fixture.customer_id,
                )
            )
            == 1
        )

    environment = SupportFixtureEnvironment(
        db_factory,
        workspace_id=str(owned.identity.workspace_id),
    ).for_case(owned.identity)
    handle = await environment.reset(seed=SEED, fixture_ref=FIXTURE_REF)
    assert handle.instance_id == owned.identity.case_id.hex
    assert environment.customer_ref(handle) == fixture.customer_ref

    opening = "Opening turn for idempotency."
    bound = await create_bound_episode_turn(
        db_factory,
        owned,
        started,
        opening=opening,
    )
    assert bound.thread.id == started.reserved_thread_id
    assert bound.turn.client_token == f"{owned.identity.case_id.hex}:0"
    async with db_factory() as session:
        stored_turn = await session.scalar(
            select(InteractiveEpisodeTurn).where(
                InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                InteractiveEpisodeTurn.turn_index == 0,
            )
        )
        assert stored_turn is not None
        assert stored_turn.thread_turn_id == bound.turn.id
        assert stored_turn.agent_run_id == bound.run.id
        assert stored_turn.dispatch_status == "BOUND"
    same_turn = await lifecycle.reserve_turn(
        owned.identity,
        turn_index=0,
        input_hash=bound.reservation.input_hash,
    )
    assert same_turn.id == bound.reservation.id
    assert same_turn.client_token == f"{owned.identity.case_id.hex}:0"
    await lifecycle.dispatch(owned.identity, turn_index=0)

    with pytest.raises(AgentHubError):
        await lifecycle.dispatch(owned.identity, turn_index=0)
    with pytest.raises(AgentHubError):
        await reserve_episode(lifecycle, owned)

    runner = ExperimentRunner(db_factory)
    async with db_factory() as session:
        await runner.prepare_run(session, run_id=owned.run.id)
        case_count = await session.scalar(
            select(func.count())
            .select_from(EvaluationExperimentCaseResult)
            .where(EvaluationExperimentCaseResult.experiment_run_id == owned.run.id)
        )
    assert case_count == 1

    async with db_factory() as session:
        run = await session.get(AgentRun, bound.run.id)
        assert run is not None
        run.status = "SUCCEEDED"
        await session.commit()
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
    assertion = await environment.assert_state(handle, {"ticket_count": 0})
    assert assertion.passed is True
    await lifecycle.retain_assertion(
        owned.identity,
        assertion={
            "passed": assertion.passed,
            "reason": assertion.reason,
            "observed": {
                "ticket_count": assertion.observed["ticket_count"],
                "ticket_statuses": assertion.observed["ticket_statuses"],
            },
        },
        source="support_fixture.assert_state",
    )
    await lifecycle.retain_observation(
        owned.identity,
        envelope=_completed_observation(bound.run.id),
    )
    await environment.teardown(handle)
    async with db_factory() as session:
        episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert episode is not None
        assert episode.cleanup_status == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
async def test_two_workspaces_and_two_repetitions_are_fully_isolated(db_factory) -> None:
    owned_cases = await create_owned_lifecycle_cases(
        db_factory,
        workspace_count=2,
        repetitions=2,
    )
    assert len(owned_cases) == 4
    assert len({item.identity.workspace_id for item in owned_cases}) == 2
    assert {
        (item.identity.workspace_id, item.identity.repetition_index) for item in owned_cases
    } == {
        (workspace_id, repetition)
        for workspace_id in {item.identity.workspace_id for item in owned_cases}
        for repetition in range(2)
    }

    lifecycle = InteractiveLifecycle(db_factory)
    episodes = []
    environments = []
    for index, owned in enumerate(owned_cases):
        episode = await reserve_episode(lifecycle, owned, seed=SEED + index)
        episode = await lifecycle.create_fixture(owned.identity)
        turn = await lifecycle.reserve_turn(
            owned.identity,
            turn_index=0,
            input_hash=f"{index + 1:064x}"[-64:],
        )
        assert turn.client_token == f"{owned.identity.case_id.hex}:0"
        episodes.append(episode)
        environment = SupportFixtureEnvironment(
            db_factory,
            workspace_id=str(owned.identity.workspace_id),
        ).for_case(owned.identity)
        handle = await environment.reset(seed=SEED + index, fixture_ref=FIXTURE_REF)
        environments.append((environment, handle))

    assert len({episode.case_id for episode in episodes}) == 4
    assert len({episode.instance_id for episode in episodes}) == 4
    assert len({episode.reserved_thread_id for episode in episodes}) == 4
    assert len({episode.customer_id for episode in episodes}) == 4
    assert len({episode.customer_ref for episode in episodes}) == 4

    async with db_factory() as session:
        for episode in episodes:
            customer = await session.scalar(
                select(Customer).where(
                    Customer.workspace_id == episode.workspace_id,
                    Customer.id == episode.customer_id,
                    Customer.customer_ref == episode.customer_ref,
                )
            )
            assert customer is not None

    primary = owned_cases[0]
    foreign_workspace = next(
        owned
        for owned in owned_cases
        if owned.identity.workspace_id != primary.identity.workspace_id
    )
    assert primary.identity.workspace_id != foreign_workspace.identity.workspace_id
    foreign = replace(
        primary.identity,
        workspace_id=foreign_workspace.identity.workspace_id,
    )
    with pytest.raises(AgentHubError) as rejected_cross_workspace:
        await lifecycle.ensure_current(foreign)
    assert rejected_cross_workspace.value.code == "INTERACTIVE_EXECUTION_FENCE_INVALID"

    for owned, episode, (environment, handle) in zip(
        owned_cases,
        episodes,
        environments,
        strict=True,
    ):
        await lifecycle.terminate(owned.identity, reason="portable_cleanup_test")
        await environment.teardown(handle)
        async with db_factory() as session:
            assert await session.get(Customer, episode.customer_id) is None
            assert owned.identity.workspace_id == episode.workspace_id


@pytest.mark.asyncio
async def test_stale_generation_cannot_bind_terminate_or_cleanup(db_factory) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    replacement_owner = f"replacement-{uuid4().hex}"
    replacement_generation = owned.identity.generation + 1
    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, owned.identity.run_id)
        case = await session.get(EvaluationExperimentCaseResult, owned.identity.case_id)
        assert run is not None and case is not None
        run.lease_owner = replacement_owner
        run.lease_generation = replacement_generation
        case.lease_owner = replacement_owner
        case.lease_generation = replacement_generation
        await session.commit()

    replacement = replace(
        owned.identity,
        owner=replacement_owner,
        generation=replacement_generation,
    )
    await lifecycle.ensure_current(replacement)
    with pytest.raises(AgentHubError):
        await lifecycle.ensure_current(owned.identity)
    with pytest.raises(AgentHubError):
        await lifecycle.reserve_turn(
            owned.identity,
            turn_index=0,
            input_hash="a" * 64,
        )
    with pytest.raises(AgentHubError):
        await lifecycle.terminate(owned.identity, reason="stale_worker")
    with pytest.raises(AgentHubError):
        await lifecycle.cleanup(owned.identity)

    version = owned.base["version"]
    thread = await ThreadService(db_factory).create_thread(
        owned.context,
        agent_id=version.agent_id,
        title="stale generation",
        thread_id=episode.reserved_thread_id,
    )
    with pytest.raises(AgentHubError):
        await lifecycle.bind_thread(owned.identity, thread_id=thread.id)

    async with db_factory() as session:
        stored = await session.get(InteractiveEpisode, owned.identity.case_id)
        customer = await session.get(Customer, episode.customer_id)
        assert stored is not None
        assert stored.thread_id is None
        assert stored.execution_status == "PREPARING"
        assert customer is not None

    await lifecycle.terminate(replacement, reason="replacement_cleanup")
    assert await lifecycle.cleanup(replacement) == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
async def test_fixture_and_ownership_binding_rollback_together(db_factory) -> None:
    owned, adjacent = await create_owned_lifecycle_cases(
        db_factory,
        workspace_count=2,
        repetitions=1,
    )
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    await reserve_episode(lifecycle, adjacent)

    async with fail_customer_mutation(
        db_factory,
        workspace_id=owned.identity.workspace_id,
        operation="insert",
    ):
        with pytest.raises(DBAPIError) as injected_failure:
            await lifecycle.create_fixture(owned.identity)
        sqlstate = getattr(injected_failure.value.orig, "sqlstate", None) or getattr(
            injected_failure.value.orig,
            "pgcode",
            None,
        )
        assert sqlstate == "P0001"
        assert "injected customer insert failure" in str(injected_failure.value.orig)
        isolated = await lifecycle.create_fixture(adjacent.identity)
        assert isolated.customer_id is not None

    async with db_factory() as session:
        episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert episode is not None
        assert episode.customer_id is None
        assert episode.customer_ref is None
        assert episode.fixture_created_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Customer)
                .where(Customer.workspace_id == owned.identity.workspace_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Customer)
                .where(Customer.workspace_id == adjacent.identity.workspace_id)
            )
            == 1
        )

    created = await lifecycle.create_fixture(owned.identity)
    assert created.customer_id is not None
    await lifecycle.terminate(owned.identity, reason="fixture_rollback_complete")
    assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_COMPLETE"
    await lifecycle.terminate(adjacent.identity, reason="adjacent_fixture_complete")
    assert await lifecycle.cleanup(adjacent.identity) == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
async def test_assertion_and_observation_survive_cleanup(db_factory) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    await lifecycle.create_fixture(owned.identity)
    assertion = {
        "passed": True,
        "observed": {"ticket_count": 0},
        "reason": None,
    }
    envelope = _completed_observation(None)

    await lifecycle.retain_assertion(
        owned.identity,
        assertion=assertion,
        source="interactive_evaluator",
    )
    await lifecycle.retain_assertion(
        owned.identity,
        assertion=assertion,
        source="interactive_evaluator",
    )
    with pytest.raises(AgentHubError):
        await lifecycle.retain_assertion(
            owned.identity,
            assertion={**assertion, "passed": False},
            source="interactive_evaluator",
        )

    await lifecycle.retain_observation(owned.identity, envelope=envelope)
    await lifecycle.retain_observation(owned.identity, envelope=envelope)
    with pytest.raises(AgentHubError):
        await lifecycle.retain_observation(
            owned.identity,
            envelope={**envelope, "latency_ms": 26},
        )

    assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_COMPLETE"
    async with db_factory() as session:
        episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert episode is not None
        assert episode.execution_status == "COMPLETED"
        assert episode.assertion == assertion
        assert episode.assertion_hash == canonical_json_hash(assertion)
        assert episode.assertion_source == "interactive_evaluator"
        assert episode.observation_envelope == envelope
        assert episode.observation_hash == canonical_json_hash(envelope)
        assert episode.cleanup_status == "TEARDOWN_COMPLETE"


@pytest.mark.asyncio
async def test_safe_cleanup_retains_recovery_fixture_snapshot_without_success(
    db_factory,
) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    assert episode.customer_id is not None
    assert episode.customer_ref is not None
    assert episode.assertion is None

    async with db_factory() as session:
        customer = await session.get(Customer, episode.customer_id)
        assert customer is not None
        ticket = Ticket(
            workspace_id=owned.identity.workspace_id,
            customer_id=customer.id,
            ticket_ref=f"AE6B-{uuid4().hex}",
            subject="Confirmed fixture state before safe termination.",
            status="OPEN",
            idempotency_key=uuid4().hex,
        )
        session.add(ticket)
        await session.commit()
        ticket_id = ticket.id
        ticket_count = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(
                Ticket.workspace_id == owned.identity.workspace_id,
                Ticket.customer_id == customer.id,
            )
        )
        assert ticket_count == 1

    await lifecycle.terminate(owned.identity, reason="interrupted_before_task_assertion")
    assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_COMPLETE"

    expected_snapshot = {
        "passed": False,
        "reason": "interrupted_before_task_assertion",
        "observed": {
            "ticket_count": 1,
            "ticket_statuses": ["OPEN"],
        },
    }
    async with db_factory() as session:
        retained = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert retained is not None
        assert retained.execution_status == "TERMINATED"
        assert retained.observation_envelope is None
        assert retained.assertion == expected_snapshot
        assert retained.assertion_hash == canonical_json_hash(expected_snapshot)
        assert retained.assertion_source == "recovery_fixture_snapshot"
        assert retained.assertion_recorded_at is not None
        assert retained.cleanup_status == "TEARDOWN_COMPLETE"
        assert await session.get(Customer, episode.customer_id) is None
        assert await session.get(Ticket, ticket_id) is None


@pytest.mark.asyncio
async def test_cleanup_failure_retains_instance_for_new_lifecycle_retry(db_factory) -> None:
    owned, adjacent = await create_owned_lifecycle_cases(
        db_factory,
        workspace_count=2,
        repetitions=1,
    )
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    await reserve_episode(lifecycle, adjacent)
    episode = await lifecycle.create_fixture(owned.identity)
    adjacent_episode = await lifecycle.create_fixture(adjacent.identity)
    await lifecycle.terminate(owned.identity, reason="cleanup_failure_test")
    await lifecycle.terminate(adjacent.identity, reason="adjacent_cleanup_test")

    async with fail_customer_mutation(
        db_factory,
        workspace_id=owned.identity.workspace_id,
        operation="delete",
    ):
        assert await lifecycle.cleanup(owned.identity) == "TEARDOWN_FAILED"
        assert await lifecycle.cleanup(adjacent.identity) == "TEARDOWN_COMPLETE"

    async with db_factory() as session:
        failed = await session.get(InteractiveEpisode, owned.identity.case_id)
        customer = await session.get(Customer, episode.customer_id)
        assert failed is not None
        assert failed.cleanup_status == "TEARDOWN_FAILED"
        assert failed.cleanup_error
        assert failed.cleanup_attempts >= 1
        assert failed.cleanup_next_attempt_at is not None
        assert failed.customer_id == episode.customer_id
        assert customer is not None
        assert await session.get(Customer, adjacent_episode.customer_id) is None

    replacement_lifecycle = InteractiveLifecycle(db_factory)
    assert await replacement_lifecycle.cleanup(owned.identity) == "TEARDOWN_COMPLETE"
    async with db_factory() as session:
        complete = await session.get(InteractiveEpisode, owned.identity.case_id)
        assert complete is not None
        assert complete.cleanup_attempts >= failed.cleanup_attempts
        assert complete.cleanup_completed_at is not None
        assert await session.get(Customer, episode.customer_id) is None


@pytest.mark.asyncio
async def test_needs_attention_blocks_cleanup_and_recovery_does_not_redispatch(
    db_factory,
) -> None:
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    lifecycle = InteractiveLifecycle(db_factory)
    await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    bound = await create_bound_episode_turn(
        db_factory,
        owned,
        episode,
        opening="An unknown side effect must be reconciled.",
    )
    await lifecycle.dispatch(owned.identity, turn_index=0)
    async with db_factory() as session:
        run = await session.get(AgentRun, bound.run.id)
        assert run is not None
        run.status = "NEEDS_ATTENTION"
        run.failure_code = "ACTION_OUTCOME_UNKNOWN"
        await session.commit()

    assert await lifecycle.cleanup(owned.identity) == "CLEANUP_BLOCKED"
    async with db_factory() as session:
        assert await session.get(Customer, episode.customer_id) is not None
        run_count = await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.workspace_id == owned.identity.workspace_id)
        )

    async with db_factory() as session:
        parent_run = await session.scalar(
            select(EvaluationExperimentRun)
            .where(EvaluationExperimentRun.id == owned.identity.run_id)
            .with_for_update()
        )
        case = await session.scalar(
            select(EvaluationExperimentCaseResult)
            .where(EvaluationExperimentCaseResult.id == owned.identity.case_id)
            .with_for_update()
        )
        assert parent_run is not None and case is not None
        recovery = await lifecycle.recover_case(session, case)
        await session.rollback()

    assert set(recovery) == {
        "envelope",
        "reason",
        "cleanup_status",
        "turns",
        "agent_run_id",
        "observed_agent_status",
        "observed_agent_failure_code",
    }
    assert recovery["envelope"] is None
    assert recovery["reason"] == "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
    assert recovery["cleanup_status"] == "CLEANUP_BLOCKED"
    assert recovery["agent_run_id"] == str(bound.run.id)
    assert recovery["observed_agent_status"] == "NEEDS_ATTENTION"
    assert recovery["observed_agent_failure_code"] == "ACTION_OUTCOME_UNKNOWN"
    assert recovery["turns"]

    async with db_factory() as session:
        assert await session.get(Customer, episode.customer_id) is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.workspace_id == owned.identity.workspace_id)
            )
            == run_count
        )
