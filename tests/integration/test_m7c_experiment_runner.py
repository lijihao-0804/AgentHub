from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.build_identity import StaticBuildIdentityProvider
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import (
    EvaluationCaseResultStatus,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRunStatus,
    EvaluationExperimentVariant,
    EvaluationMetricResult,
    EvaluationMetricSnapshot,
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
            cached_tokens=0,
        )


class _ObservedFailureDriver:
    async def execute(self, session, *, run, variant, item) -> CaseExecutionObservation:
        del session, run, variant, item
        return CaseExecutionObservation(
            observation={
                "category": "FAILURE",
                "observed_agent_status": "FAILED",
                "observed_agent_failure_code": "MODEL_TIMEOUT",
            },
            failure_code="MODEL_TIMEOUT",
        )


async def _ready_run(
    session: AsyncSession,
    base: dict[str, object],
    *,
    case_count: int = 2,
    repetitions: int = 1,
):
    context = _manager_context(base)
    _, dataset_version = await _published_version(
        session,
        base,
        items=[
            _item(f"case-{ordinal}", split="DEV", ordinal=ordinal)
            for ordinal in range(case_count)
        ],
    )
    pricing = await _pricing(session, base)
    service = ExperimentService(StaticBuildIdentityProvider("1" * 40))
    experiment = await service.create_experiment(
        session,
        context=context,
        name=f"runner-{uuid4().hex}",
        description=None,
        dataset_version_id=dataset_version.id,
        split="DEV",
        purpose="DEVELOPMENT",
        repetitions=repetitions,
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

    async with db_factory() as verification_session:
        results = list(
            await verification_session.scalars(
                select(EvaluationExperimentCaseResult)
                .where(EvaluationExperimentCaseResult.experiment_run_id == run.id)
                .order_by(
                    EvaluationExperimentCaseResult.created_at,
                    EvaluationExperimentCaseResult.id,
                )
            )
        )
        stored_run = await verification_session.get(type(run), run.id)
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


@pytest.mark.asyncio
async def test_m7c_expected_agent_failure_is_not_runner_failure(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7c-observed-{uuid4().hex}")
        run = await _ready_run(session, base)
    runner = ExperimentRunner(db_factory, driver=_ObservedFailureDriver())
    await runner.execute(
        run_id=run.id, owner="m7c-observed-worker", settings=Settings(testing=True)
    )

    async with db_factory() as session:
        rows = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.experiment_run_id == run.id
                )
            )
        )
        stored_run = await session.get(type(run), run.id)
    assert stored_run is not None
    assert stored_run.status == EvaluationExperimentRunStatus.SUCCEEDED
    assert rows
    assert all(row.status == EvaluationCaseResultStatus.SUCCEEDED for row in rows)
    assert all(row.failure_code is None for row in rows)
    assert all(row.observed_agent_failure_code == "MODEL_TIMEOUT" for row in rows)


@pytest.mark.asyncio
async def test_m7c_cancelled_queued_run_does_not_materialize_cases(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7c-cancel-{uuid4().hex}")
        run = await _ready_run(session, base)
        runner = ExperimentRunner(db_factory)
        assert await runner.cancel(session, run_id=run.id)
    await runner.execute(
        run_id=run.id, owner="m7c-cancel-worker", settings=Settings(testing=True)
    )

    async with db_factory() as session:
        stored_run = await session.get(type(run), run.id)
        case_count = await session.scalar(
            select(func.count(EvaluationExperimentCaseResult.id)).where(
                EvaluationExperimentCaseResult.experiment_run_id == run.id
            )
        )
    assert stored_run is not None
    assert stored_run.status == EvaluationExperimentRunStatus.CANCELLED
    assert case_count == 0


@pytest.mark.asyncio
async def test_m7d_metrics_and_comparison_use_persisted_m7c_cases(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"m7d-metrics-{uuid4().hex}")
        run = await _ready_run(session, base, case_count=10, repetitions=3)
        context = _manager_context(base)
    runner = ExperimentRunner(db_factory, driver=_CountingDriver())
    await runner.execute(run_id=run.id, owner="m7d-metrics-worker", settings=Settings(testing=True))

    async with db_factory() as session:
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.experiment_id == run.experiment_id
                )
            )
        )
        await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.experiment_run_id == run.id,
                EvaluationExperimentCaseResult.experiment_variant_id == variants[1].id,
            )
            .values(cost_currency="EUR")
        )
        await session.commit()
        service = EvaluationMetricsService()
        metrics = await service.materialize_metrics(session, context=context, run_id=run.id)
        repeated = await service.materialize_metrics(session, context=context, run_id=run.id)
        persisted = await service.get_persisted_metrics(session, context=context, run_id=run.id)
        viewer = context.model_copy(
            update={"workspace_role": "VIEWER", "permissions": frozenset({"evaluation_read"})}
        )
        snapshot_count_before = await session.scalar(
            select(func.count(EvaluationMetricSnapshot.id)).where(
                EvaluationMetricSnapshot.experiment_run_id == run.id
            )
        )
        metric_count_before = await session.scalar(
            select(func.count(EvaluationMetricResult.id)).where(
                EvaluationMetricResult.experiment_run_id == run.id
            )
        )
        viewer_metrics = await service.get_persisted_metrics(
            session, context=viewer, run_id=run.id
        )
        with pytest.raises(AgentHubError) as viewer_materialize:
            await service.materialize_metrics(session, context=viewer, run_id=run.id)
        assert viewer_materialize.value.status_code == 403
        assert viewer_metrics["metrics_hash"] == metrics["metrics_hash"]
        assert (
            await session.scalar(
                select(func.count(EvaluationMetricSnapshot.id)).where(
                    EvaluationMetricSnapshot.experiment_run_id == run.id
                )
            )
            == snapshot_count_before
        )
        assert (
            await session.scalar(
                select(func.count(EvaluationMetricResult.id)).where(
                    EvaluationMetricResult.experiment_run_id == run.id
                )
            )
            == metric_count_before
        )
        comparison = await service.create_comparison(
            session,
            context=context,
            run_id=run.id,
            baseline_variant_id=variants[0].id,
            candidate_variant_id=variants[1].id,
        )
        comparison_again = await service.create_comparison(
            session,
            context=context,
            run_id=run.id,
            baseline_variant_id=variants[0].id,
            candidate_variant_id=variants[1].id,
        )
        viewer_comparisons = await service.get_comparisons(
            session, context=viewer, run_id=run.id
        )
        with pytest.raises(AgentHubError) as viewer_comparison:
            await service.create_comparison(
                session,
                context=viewer,
                run_id=run.id,
                baseline_variant_id=variants[0].id,
                candidate_variant_id=variants[1].id,
            )
    assert set(metrics["variants"]) == {str(variant.id) for variant in variants}
    assert all(len(metrics["variants"][str(variant.id)]["categories"]) == 1 for variant in variants)
    assert all(
        metrics["variants"][str(variant.id)]["total_tokens"]["value"] == 4500
        for variant in variants
    )
    assert all(
        metrics["variants"][str(variant.id)]["task_success"]["sample_count"] == 10
        for variant in variants
    )
    assert repeated["snapshot_id"] == metrics["snapshot_id"]
    assert repeated["metrics_hash"] == metrics["metrics_hash"]
    assert persisted["metrics_hash"] == metrics["metrics_hash"]
    assert comparison.status == "COMPLETE"
    assert comparison.paired_pairs == 10
    assert comparison.missing_pairs == 0
    assert comparison.metrics["cost_per_successful_case"]["status"] == "NOT_COMPARABLE"
    assert comparison.metrics["cost_per_successful_case"]["reason"] == "currency_mismatch"
    assert comparison_again.id == comparison.id
    assert comparison_again.comparison_hash == comparison.comparison_hash
    assert len(viewer_comparisons) == 1
    assert viewer_comparison.value.status_code == 403
