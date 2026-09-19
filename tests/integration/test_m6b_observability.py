from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.observability.metrics import MetricsQueryService
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m6a_run_observability import _add_approval, _add_run, _read_context

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M6-B PostgreSQL integration tests.",
    ),
]


def _async_database_url(database_url: str) -> str:
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
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_m6b_summary_denominators_latency_usage_cost_and_current_state(db_factory) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with db_factory() as session:
        base = await _seed(session, label=f"m6b-summary-{uuid4().hex}")
        durations = (100, 200, 300, 400)
        statuses = ("SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION")
        runs = []
        for index, (status, duration) in enumerate(zip(statuses, durations, strict=True)):
            run = await _add_run(
                session,
                base,
                status=status,
                failure_code="MODEL_PROVIDER_FAILED"
                if status == "FAILED"
                else ("ACTION_RECONCILIATION_REQUIRED" if status == "NEEDS_ATTENTION" else None),
                created_at=start + timedelta(hours=index),
            )
            run.started_at = start + timedelta(hours=index)
            run.completed_at = run.started_at + timedelta(milliseconds=duration)
            run.total_tokens = 100 + index
            run.total_input_tokens = 60 + index
            run.total_output_tokens = 40
            run.total_cached_tokens = 10
            run.total_cost_amount = "1.00"
            run.cost_currency = "USD"
            runs.append(run)
        waiting = await _add_run(
            session,
            base,
            status="WAITING_APPROVAL",
            created_at=start + timedelta(hours=5),
        )
        waiting.started_at = start + timedelta(hours=5)
        waiting.completed_at = None
        waiting.total_tokens = None
        waiting.total_input_tokens = None
        waiting.total_output_tokens = None
        waiting.total_cached_tokens = None
        waiting.total_cost_amount = None
        waiting.cost_currency = None
        await _add_approval(
            session,
            base,
            waiting,
            decision_status="PENDING",
            execution_status="NOT_STARTED",
            created_at=start + timedelta(hours=5),
        )
        unknown = await _add_approval(
            session,
            base,
            runs[-1],
            decision_status="APPROVED",
            execution_status="UNKNOWN_OUTCOME",
            created_at=start + timedelta(hours=3),
            decided_at=start + timedelta(hours=3, seconds=2),
            executed_at=start + timedelta(hours=3, seconds=3),
            failure_code="ACTION_OUTCOME_UNKNOWN",
        )
        unknown.expires_at = start + timedelta(hours=4)
        await session.commit()

    service = MetricsQueryService(db_factory)
    query_count = 0
    bind = db_factory.kw["bind"]

    def count_query(*args) -> None:
        del args
        nonlocal query_count
        query_count += 1

    event.listen(bind.sync_engine, "before_cursor_execute", count_query)
    try:
        summary = await service.summary(_read_context(base), start=start, end=end)
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", count_query)

    assert query_count <= 6
    assert summary["finished_runs"] == {
        "succeeded": 1,
        "failed": 1,
        "cancelled": 1,
        "needs_attention": 1,
        "denominator": 4,
    }
    assert summary["success_rate"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
    assert summary["failure_rate"]["rate"] == 0.25
    assert summary["latency"]["p50_ms"] == 250.0
    assert summary["latency"]["p95_ms"] == 385.0
    assert summary["latency"]["sample_count"] == 4
    assert summary["usage"]["known_usage_count"] == 4
    assert summary["usage"]["unknown_usage_count"] == 1
    assert summary["cost"]["currency"] == "USD"
    assert summary["cost"]["successful_cost_denominator"] == 1
    assert summary["current"]["waiting_approval_count"] >= 1
    assert summary["current"]["unknown_outcome_action_count"] >= 1
    assert summary["approvals"]["pending"] == 1
    assert summary["approvals"]["unknown_outcome"] == 1
    assert str(unknown.id) not in str(summary)


@pytest.mark.asyncio
async def test_m6b_failure_analytics_preserves_codes_and_agent_version_breakdown(
    db_factory,
) -> None:
    start = datetime(2026, 2, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with db_factory() as session:
        base = await _seed(session, label=f"m6b-failures-{uuid4().hex}")
        model_failed = await _add_run(
            session,
            base,
            status="FAILED",
            failure_code="MODEL_PROVIDER_FAILED",
            created_at=start,
        )
        model_failed.started_at = start
        model_failed.completed_at = start + timedelta(milliseconds=100)
        attention = await _add_run(
            session,
            base,
            status="NEEDS_ATTENTION",
            failure_code="ACTION_RECONCILIATION_REQUIRED",
            created_at=start + timedelta(hours=1),
        )
        attention.started_at = start + timedelta(hours=1)
        attention.completed_at = attention.started_at + timedelta(milliseconds=200)
        approval = await _add_approval(
            session,
            base,
            attention,
            decision_status="APPROVED",
            execution_status="UNKNOWN_OUTCOME",
            created_at=start + timedelta(hours=1),
            decided_at=start + timedelta(hours=1, seconds=1),
            executed_at=start + timedelta(hours=1, seconds=2),
            failure_code="ACTION_OUTCOME_UNKNOWN",
        )
        await session.commit()

    service = MetricsQueryService(db_factory)
    failures = await service.failure_analytics(_read_context(base), start=start, end=end)
    categories = {item["failure_category"]: item for item in failures["categories"]}
    assert categories["MODEL"]["count"] == 1
    assert categories["MODEL"]["top_failure_codes"] == [
        {"failure_code": "MODEL_PROVIDER_FAILED", "count": 1}
    ]
    assert categories["ACTION"]["count"] == 1
    assert failures["items"][0]["approval_id"] == approval.id
    assert failures["items"][0]["action_failure_code"] == "ACTION_OUTCOME_UNKNOWN"
    assert "raw" not in str(failures).lower()

    versions = await service.agent_version_breakdown(_read_context(base), start=start, end=end)
    assert versions["items"][0]["run_count"] == 2
    assert versions["items"][0]["failed_count"] == 1
    assert versions["items"][0]["needs_attention_count"] == 1

    series = await service.timeseries(_read_context(base), start=start, end=end, bucket="day")
    assert len(series["items"]) == 1
    assert series["items"][0]["runs"] == 2
    assert series["items"][0]["failed"] == 1


@pytest.mark.asyncio
async def test_m6b_cross_workspace_and_viewer_isolation(db_factory) -> None:
    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    async with db_factory() as session:
        first = await _seed(session, label=f"m6b-a-{uuid4().hex}")
        second = await _seed(session, label=f"m6b-b-{uuid4().hex}")
        await _add_run(
            session, second, status="FAILED", failure_code="TOOL_FAILED", created_at=start
        )
        await session.commit()

    service = MetricsQueryService(db_factory)
    first_summary = await service.summary(_read_context(first, role="VIEWER"), start=start, end=end)
    first_failures = await service.failure_analytics(
        _read_context(first, role="VIEWER"), start=start, end=end
    )
    assert first_summary["finished_runs"]["denominator"] == 0
    assert first_failures["items"] == []
    assert first_failures["categories"] == []

    with pytest.raises(AgentHubError) as raised:
        await service.summary(_read_context(first), start=start, end=end + timedelta(days=91))
    assert raised.value.code == "INVALID_OBSERVABILITY_WINDOW"
