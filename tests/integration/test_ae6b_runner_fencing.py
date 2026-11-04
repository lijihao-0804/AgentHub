from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.evaluation.models import EvaluationExperimentCaseResult, EvaluationExperimentRun
from packages.evaluation.runner import CaseExecutionObservation, ExperimentRunner
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database
from tests.support.ae6b_lifecycle import create_owned_lifecycle_cases

__all__ = ["db_factory", "migrated_database"]
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_expired_parent_lease_cannot_renew_bind_complete_or_fail_a_case(db_factory):
    owned = (await create_owned_lifecycle_cases(db_factory, repetitions=1))[0]
    runner = ExperimentRunner(db_factory)
    async with db_factory() as session:
        run = await session.get(EvaluationExperimentRun, owned.run.id)
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert not await runner.heartbeat(
            session,
            run_id=run.id,
            owner=owned.identity.owner,
            generation=owned.identity.generation,
            lease_seconds=30,
        )
        assert not await runner.bind_case_agent_run(
            session,
            case_id=owned.case.id,
            run_id=run.id,
            agent_run_id=uuid4(),
            owner=owned.identity.owner,
            generation=owned.identity.generation,
        )
        assert not await runner.complete_case(
            session,
            case_id=owned.case.id,
            run_id=run.id,
            owner=owned.identity.owner,
            generation=owned.identity.generation,
            observation=CaseExecutionObservation(observation={"category": "INTERACTIVE"}),
        )
        assert not await runner.fail_case(
            session,
            case_id=owned.case.id,
            run_id=run.id,
            owner=owned.identity.owner,
            generation=owned.identity.generation,
            failure_code="STALE_WORKER",
            safe_message="Obsolete worker.",
        )
        stored = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.id == owned.case.id
            )
        )
        assert stored.status == "RUNNING"
        assert stored.agent_run_id is None
        assert stored.failure_code is None
