from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.models import (
    EvaluationCaseResultStatus,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRunStatus,
)
from packages.evaluation.runner import (
    CaseExecutionObservation,
    ExperimentRunner,
)
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m7b_experiments import (
    _item,
    _manager_context,
    _pricing,
    _published_version,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7-C PostgreSQL integration tests.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_factory(migrated_database: None) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class _CountingDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, int]] = []

    async def execute(self, session, *, run, variant, item) -> CaseExecutionObservation:
        del session
        self.calls.append((variant.id, item.id, len(self.calls)))
        return CaseExecutionObservation(
            observation={"category": item.category, "variant_hash": variant.variant_hash},
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )


async def _ready_run(session: AsyncSession, base: dict[str, object]):
    context = _manager_context(base)
    _, dataset_version = await _published_version(
        session,
        base,
        items=[
            _item("case-0", split="DEV", ordinal=0),
            _item("case-1", split="DEV", ordinal=1),
        ],
    )
    pricing = await _pricing(session, base)
    service = ExperimentService(StaticBuildIdentityProvider("m" * 40))
    experiment = await service.create_experiment(
        session,
        context=context,
        name=f"runner-{uuid4().hex}",
        description=None,
        dataset_version_id=dataset_version.id,
        split="DEV",
        purpose="DEVELOPMENT",
    )
    for ordinal in range(2):
        await service.add_variant(
            session,
            context=context,
            experiment_id=experiment.id,
            label=f"variant-{ordinal}",
            agent_version_id=base["version"].id,
            pricing_snapshot_id=pricing.id,
            ordinal=ordinal,
        )
    await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
    run, _ = await service.create_run(session, context=context, experiment_id=experiment.id)
    return run


@pytest.mark.asyncio
async def test_m7c_deterministic_plan_execution_and_cost_freeze(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7c-runner-{uuid4().hex}")
        run = await _ready_run(session, base)
        driver = _CountingDriver()
        runner = ExperimentRunner(db_factory, driver=driver)
        await runner.execute(
            run_id=run.id,
            owner="m7c-test-worker",
            settings=Settings(testing=True),
        )

        results = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult)
                .where(EvaluationExperimentCaseResult.experiment_run_id == run.id)
                .order_by(
                    EvaluationExperimentCaseResult.created_at,
                    EvaluationExperimentCaseResult.id,
                )
            )
        )
        stored_run = await session.get(type(run), run.id)
        assert len(results) == 4
        assert all(row.status == EvaluationCaseResultStatus.SUCCEEDED for row in results)
        assert all(row.total_tokens == 150 for row in results)
        assert all(row.cost_amount == Decimal("0.00002000") for row in results)
        assert stored_run is not None
        assert stored_run.status == EvaluationExperimentRunStatus.SUCCEEDED
        assert len(driver.calls) == 4
        assert all("raw" not in str(row.observation).lower() for row in results)


@pytest.mark.asyncio
async def test_m7c_duplicate_delivery_has_one_atomic_run_claim(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7c-claim-{uuid4().hex}")
        run = await _ready_run(session, base)
    first_engine, first_factory = create_database(async_database_url(TEST_DATABASE_URL))
    second_engine, second_factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        first = ExperimentRunner(first_factory)
        second = ExperimentRunner(second_factory)
        async with first_factory() as first_session:
            first_claimed = await first.claim_run(
                first_session,
                run_id=run.id,
                owner="worker-a",
                lease_seconds=300,
            )
        assert first_claimed
        async with second_factory() as second_session:
            second_claimed = await second.claim_run(
                second_session,
                run_id=run.id,
                owner="worker-b",
                lease_seconds=300,
            )
        assert not second_claimed
    finally:
        await first_engine.dispose()
        await second_engine.dispose()
