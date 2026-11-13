"""Real-PostgreSQL claim locking and terminality regressions for AE-6b."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
)
from packages.evaluation.runner import CaseExecutionObservation, ExperimentRunner
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import (
    InteractiveExperimentSetup,
    create_interactive_driver_experiment,
)

__all__ = ["db_factory", "migrated_database"]
pytestmark = pytest.mark.integration


class ForbiddenDriver:
    """Prove that claim and finish operations never execute the case driver."""

    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, *args, **kwargs):
        del args, kwargs
        self.execute_calls += 1
        raise AssertionError("AE6B_CLAIM_LOCK_MUST_NOT_EXECUTE_PROVIDER")


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    setup: InteractiveExperimentSetup
    runner: ExperimentRunner
    driver: ForbiddenDriver
    owner: str
    generation: int
    case_id: UUID


async def _prepare_and_claim_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClaimedRun:
    setup = await create_interactive_driver_experiment(session_factory)
    driver = ForbiddenDriver()
    runner = ExperimentRunner(session_factory, driver=driver)
    owner = "ae6b-claim-lock-" + uuid4().hex

    async with session_factory() as session:
        prepared = await runner.prepare_run(session, run_id=setup.run.id)
    assert prepared == 1

    async with session_factory() as session:
        claimed = await runner.claim_run(
            session,
            run_id=setup.run.id,
            owner=owner,
            lease_seconds=1_800,
        )
    assert claimed is True

    async with session_factory() as session:
        run = await session.get(EvaluationExperimentRun, setup.run.id)
        assert run is not None
        assert run.status == "RUNNING"
        assert run.lease_owner == owner
        assert run.lease_generation is not None
        generation = run.lease_generation
        case_id = await session.scalar(
            select(EvaluationExperimentCaseResult.id).where(
                EvaluationExperimentCaseResult.experiment_run_id == run.id
            )
        )
    assert case_id is not None
    assert driver.execute_calls == 0
    return ClaimedRun(
        setup=setup,
        runner=runner,
        driver=driver,
        owner=owner,
        generation=generation,
        case_id=case_id,
    )


@pytest.mark.asyncio
async def test_claim_case_does_not_lock_on_held_parent_row(db_factory) -> None:
    claimed_run = await _prepare_and_claim_run(db_factory)

    async with db_factory() as parent_lock, db_factory() as claim_session:
        parent = await parent_lock.scalar(
            select(EvaluationExperimentRun)
            .where(EvaluationExperimentRun.id == claimed_run.setup.run.id)
            .with_for_update()
        )
        assert parent is not None
        assert parent.status == "RUNNING"

        case = await asyncio.wait_for(
            claimed_run.runner.claim_case(
                claim_session,
                run_id=claimed_run.setup.run.id,
                owner=claimed_run.owner,
                generation=claimed_run.generation,
            ),
            timeout=2,
        )
        assert case is not None
        assert case.id == claimed_run.case_id
        assert case.status == "RUNNING"
        assert case.lease_owner == claimed_run.owner
        assert case.lease_generation == claimed_run.generation

    assert claimed_run.driver.execute_calls == 0


@pytest.mark.asyncio
async def test_finish_run_keeps_pending_case_and_parent_running(db_factory) -> None:
    claimed_run = await _prepare_and_claim_run(db_factory)

    async with db_factory() as session:
        finished = await claimed_run.runner._finish_run(
            session,
            run_id=claimed_run.setup.run.id,
            owner=claimed_run.owner,
            generation=claimed_run.generation,
        )
    assert finished is False

    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, claimed_run.setup.run.id)
        case = await session.get(EvaluationExperimentCaseResult, claimed_run.case_id)
    assert run is not None
    assert case is not None
    assert run.status == "RUNNING"
    assert run.completed_at is None
    assert run.lease_owner == claimed_run.owner
    assert case.status == "PENDING"
    assert case.completed_at is None
    assert claimed_run.driver.execute_calls == 0


@pytest.mark.asyncio
async def test_expired_parent_cannot_claim_or_finish_pending_case(db_factory) -> None:
    claimed_run = await _prepare_and_claim_run(db_factory)

    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, claimed_run.setup.run.id)
        assert run is not None
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    for _ in range(3):
        async with db_factory() as session:
            case = await asyncio.wait_for(
                claimed_run.runner.claim_case(
                    session,
                    run_id=claimed_run.setup.run.id,
                    owner=claimed_run.owner,
                    generation=claimed_run.generation,
                ),
                timeout=2,
            )
        assert case is None

        async with db_factory() as session:
            finished = await claimed_run.runner._finish_run(
                session,
                run_id=claimed_run.setup.run.id,
                owner=claimed_run.owner,
                generation=claimed_run.generation,
            )
        assert finished is False

    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, claimed_run.setup.run.id)
        case = await session.get(EvaluationExperimentCaseResult, claimed_run.case_id)
    assert run is not None
    assert case is not None
    assert run.status == "RUNNING"
    assert run.lease_owner == claimed_run.owner
    assert run.lease_generation == claimed_run.generation
    assert run.lease_expires_at is not None
    assert run.lease_expires_at < datetime.now(UTC)
    assert case.status == "PENDING"
    assert case.completed_at is None
    assert claimed_run.driver.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    ["FAILED", "SUCCEEDED"],
    ids=["failed", "succeeded"],
)
async def test_case_lock_is_not_terminal_and_release_allows_claimed_cleanup(
    db_factory,
    terminal_status,
) -> None:
    claimed_run = await _prepare_and_claim_run(db_factory)

    async with db_factory() as case_lock, db_factory() as claim_session:
        locked_case = await case_lock.scalar(
            select(EvaluationExperimentCaseResult)
            .where(EvaluationExperimentCaseResult.id == claimed_run.case_id)
            .with_for_update()
        )
        assert locked_case is not None
        assert locked_case.status == "PENDING"

        blocked_claim = await asyncio.wait_for(
            claimed_run.runner.claim_case(
                claim_session,
                run_id=claimed_run.setup.run.id,
                owner=claimed_run.owner,
                generation=claimed_run.generation,
            ),
            timeout=2,
        )
        assert blocked_claim is None
        finished = await claimed_run.runner._finish_run(
            claim_session,
            run_id=claimed_run.setup.run.id,
            owner=claimed_run.owner,
            generation=claimed_run.generation,
        )
        assert finished is False

        async with db_factory() as observer:
            run = await observer.get(EvaluationExperimentRun, claimed_run.setup.run.id)
            case = await observer.get(EvaluationExperimentCaseResult, claimed_run.case_id)
        assert run is not None
        assert case is not None
        assert run.status == "RUNNING"
        assert case.status == "PENDING"

    async with db_factory() as session:
        case = await asyncio.wait_for(
            claimed_run.runner.claim_case(
                session,
                run_id=claimed_run.setup.run.id,
                owner=claimed_run.owner,
                generation=claimed_run.generation,
            ),
            timeout=2,
        )
    assert case is not None
    assert case.id == claimed_run.case_id
    assert case.status == "RUNNING"

    async with db_factory() as session:
        if terminal_status == "FAILED":
            transitioned = await claimed_run.runner.fail_case(
                session,
                case_id=claimed_run.case_id,
                run_id=claimed_run.setup.run.id,
                owner=claimed_run.owner,
                generation=claimed_run.generation,
                failure_code="AE6B_CASE_LOCK_RELEASED",
                safe_message="The case lock was released and the claimed failure completed.",
            )
        else:
            transitioned = await claimed_run.runner.complete_case(
                session,
                case_id=claimed_run.case_id,
                run_id=claimed_run.setup.run.id,
                owner=claimed_run.owner,
                generation=claimed_run.generation,
                observation=CaseExecutionObservation(
                    observation={
                        "category": "INTERACTIVE",
                        "observed_agent_status": terminal_status,
                    }
                ),
            )
        assert transitioned is True
        finished = await claimed_run.runner._finish_run(
            session,
            run_id=claimed_run.setup.run.id,
            owner=claimed_run.owner,
            generation=claimed_run.generation,
        )
    assert finished is True

    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, claimed_run.setup.run.id)
        case = await session.get(EvaluationExperimentCaseResult, claimed_run.case_id)
    assert run is not None
    assert case is not None
    assert run.status == terminal_status
    assert run.completed_at is not None
    assert run.lease_owner is None
    assert run.lease_expires_at is None
    assert case.status == terminal_status
    assert case.completed_at is not None
    assert (case.failure_code is not None) is (terminal_status == "FAILED")
    assert claimed_run.driver.execute_calls == 0
