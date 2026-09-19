from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, RunStep
from packages.approvals.models import Approval
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.observability.runs import RunQueryService
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M6-A PostgreSQL integration tests.",
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


async def _add_run(
    session: AsyncSession,
    base: dict[str, object],
    *,
    status: str,
    created_at: datetime,
    failure_code: str | None = None,
    with_sensitive_values: bool = False,
) -> AgentRun:
    run = AgentRun(
        workspace_id=base["workspace_id"],
        agent_version_id=base["version"].id,
        status=status,
        input_text=(
            "raw customer prompt that must not be projected" if with_sensitive_values else "safe"
        ),
        final_output=(
            "raw model output that must not be projected" if with_sensitive_values else None
        ),
        failure_code=failure_code,
        resolved_spec_hash=base["version"].resolved_spec_hash,
        effective_knowledge_snapshots=[
            {
                "snapshot_id": str(uuid4()),
                "snapshot_hash": "a" * 64,
                "raw_content": "must not be projected",
            }
        ],
        model_step_count=2,
        tool_call_count=1,
        total_input_tokens=12,
        total_output_tokens=8,
        total_tokens=20,
        total_cached_tokens=2,
        total_cost_amount="0.12340000",
        cost_currency="USD",
        cost_is_estimate=True,
        created_by=base["user"].id,
        created_at=created_at,
        started_at=created_at + timedelta(milliseconds=10),
        completed_at=(created_at + timedelta(seconds=2) if status != "WAITING_APPROVAL" else None),
    )
    session.add(run)
    await session.flush()
    return run


async def _add_step(
    session: AsyncSession,
    base: dict[str, object],
    run: AgentRun,
    *,
    sequence: int,
    kind: str,
    status: str,
    created_at: datetime,
    safe_metadata: dict[str, object] | None = None,
) -> RunStep:
    step = RunStep(
        workspace_id=base["workspace_id"],
        agent_run_id=run.id,
        sequence_number=sequence,
        kind=kind,
        status=status,
        safe_metadata=safe_metadata or {},
        created_at=created_at,
    )
    session.add(step)
    await session.flush()
    return step


async def _add_approval(
    session: AsyncSession,
    base: dict[str, object],
    run: AgentRun,
    *,
    decision_status: str,
    execution_status: str,
    created_at: datetime,
    decided_at: datetime | None = None,
    executed_at: datetime | None = None,
    failure_code: str | None = None,
) -> Approval:
    approval = Approval(
        workspace_id=base["workspace_id"],
        run_id=run.id,
        agent_version_id=base["version"].id,
        logical_action_id=f"action-{uuid4().hex}",
        tool_identity="create_ticket",
        canonical_arguments={"secret_customer": "must not be projected"},
        canonical_args_hash="b" * 64,
        decision_status=decision_status,
        execution_status=execution_status,
        requested_by=base["user"].id,
        decided_at=decided_at,
        executed_at=executed_at,
        failure_code=failure_code,
        safe_result={"secret_result": "must not be projected"},
        idempotency_key=f"idem-{uuid4().hex}",
        created_at=created_at,
        updated_at=executed_at or decided_at or created_at,
    )
    session.add(approval)
    await session.flush()
    return approval


def _read_context(base: dict[str, object], *, role: str = "VIEWER"):
    return base["context"].model_copy(
        update={"workspace_role": role, "permissions": frozenset({"workspace_read"})}
    )


@pytest.mark.asyncio
async def test_m6a_list_detail_viewer_pagination_and_single_list_query(db_factory) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with db_factory() as session:
        base = await _seed(session, label=f"m6a-list-{uuid4().hex}")
        older = await _add_run(
            session,
            base,
            status="SUCCEEDED",
            created_at=now,
            with_sensitive_values=True,
        )
        newer = await _add_run(
            session,
            base,
            status="SUCCEEDED",
            created_at=now + timedelta(minutes=1),
        )
        await session.commit()

    service = RunQueryService(db_factory)
    context = _read_context(base)
    bind = db_factory.kw["bind"]
    query_count = 0

    def count_query(*args):
        del args
        nonlocal query_count
        query_count += 1

    event.listen(bind.sync_engine, "before_cursor_execute", count_query)
    try:
        items, cursor = await service.list_runs(context, limit=1)
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", count_query)

    assert query_count == 1
    assert [item["id"] for item in items] == [newer.id]
    assert cursor is not None
    next_items, next_cursor = await service.list_runs(context, limit=1, cursor=cursor)
    assert [item["id"] for item in next_items] == [older.id]
    assert next_cursor is None

    detail = await service.get_detail(context, newer.id)
    assert detail["status"] == "SUCCEEDED"
    assert detail["agent_version_number"] == 1
    assert detail["total_tokens"] == 20
    assert detail["total_cost_amount"] == Decimal("0.12340000")
    assert "input_text" not in detail
    assert "final_output" not in detail
    assert detail["effective_knowledge_snapshots"] == [
        {
            "snapshot_id": detail["effective_knowledge_snapshots"][0]["snapshot_id"],
            "snapshot_hash": "a" * 64,
        }
    ]

    viewer_detail = await service.get_detail(_read_context(base, role="VIEWER"), newer.id)
    assert viewer_detail["id"] == newer.id


@pytest.mark.asyncio
async def test_m6a_cross_workspace_resource_isolation(db_factory) -> None:
    now = datetime.now(UTC)
    async with db_factory() as session:
        first = await _seed(session, label=f"m6a-a-{uuid4().hex}")
        second = await _seed(session, label=f"m6a-b-{uuid4().hex}")
        run = await _add_run(session, second, status="SUCCEEDED", created_at=now)
        await session.commit()

    service = RunQueryService(db_factory)
    items, _ = await service.list_runs(_read_context(first))
    assert items == []
    with pytest.raises(AgentHubError) as raised:
        await service.get_detail(_read_context(first), run.id)
    assert raised.value.code == "AGENT_RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_m6a_timeline_ordering_and_approval_action_distinction(db_factory) -> None:
    created_at = datetime.now(UTC).replace(microsecond=0)
    async with db_factory() as session:
        base = await _seed(session, label=f"m6a-timeline-{uuid4().hex}")
        run = await _add_run(
            session,
            base,
            status="NEEDS_ATTENTION",
            failure_code="UNKNOWN_OUTCOME",
            created_at=created_at,
        )
        await _add_step(
            session,
            base,
            run,
            sequence=1,
            kind="MODEL",
            status="SUCCEEDED",
            created_at=created_at + timedelta(seconds=1),
            safe_metadata={"model_round": 1, "tool_count": 1},
        )
        await _add_step(
            session,
            base,
            run,
            sequence=2,
            kind="TOOL_EXECUTE",
            status="SUCCEEDED",
            created_at=created_at + timedelta(seconds=2),
            safe_metadata={"tool_identities": ["search_knowledge"]},
        )
        await _add_step(
            session,
            base,
            run,
            sequence=3,
            kind="APPROVAL_WAIT",
            status="WAITING",
            created_at=created_at + timedelta(seconds=3),
            safe_metadata={"policy_decision": "REQUIRE_APPROVAL"},
        )
        approval = await _add_approval(
            session,
            base,
            run,
            decision_status="APPROVED",
            execution_status="UNKNOWN_OUTCOME",
            created_at=created_at + timedelta(seconds=3),
            decided_at=created_at + timedelta(seconds=4),
            executed_at=created_at + timedelta(seconds=5),
            failure_code="UNKNOWN_OUTCOME",
        )
        await session.commit()

    timeline = await RunQueryService(db_factory).get_timeline(_read_context(base), run.id)
    kinds = [entry["kind"] for entry in timeline]
    assert kinds[:4] == ["RUN_STARTED", "MODEL", "RETRIEVAL", "APPROVAL_WAIT"]
    assert "APPROVAL_DECISION" in kinds
    assert "ACTION_EXECUTION" in kinds
    decision = next(entry for entry in timeline if entry["kind"] == "APPROVAL_DECISION")
    execution = next(entry for entry in timeline if entry["kind"] == "ACTION_EXECUTION")
    assert decision["status"] == "APPROVED"
    assert execution["status"] == "UNKNOWN_OUTCOME"
    assert execution["failure_code"] == "UNKNOWN_OUTCOME"
    assert decision["metadata"].get("approval_id") == str(approval.id)
    assert all(
        "secret_customer" not in str(entry) and "secret_result" not in str(entry)
        for entry in timeline
    )
    assert [entry["sequence"] for entry in timeline] == list(range(1, len(timeline) + 1))


@pytest.mark.asyncio
async def test_m6a_waiting_approval_projection_is_distinct_from_needs_attention(db_factory) -> None:
    now = datetime.now(UTC)
    async with db_factory() as session:
        base = await _seed(session, label=f"m6a-status-{uuid4().hex}")
        waiting = await _add_run(session, base, status="WAITING_APPROVAL", created_at=now)
        await _add_approval(
            session,
            base,
            waiting,
            decision_status="PENDING",
            execution_status="NOT_STARTED",
            created_at=now,
        )
        attention = await _add_run(
            session,
            base,
            status="NEEDS_ATTENTION",
            failure_code="UNKNOWN_OUTCOME",
            created_at=now + timedelta(seconds=1),
        )
        await session.commit()

    service = RunQueryService(db_factory)
    items, _ = await service.list_runs(_read_context(base))
    by_id = {item["id"]: item for item in items}
    assert by_id[waiting.id]["status"] == "WAITING_APPROVAL"
    assert by_id[waiting.id]["approval_summary"]["pending"] == 1
    assert by_id[attention.id]["status"] == "NEEDS_ATTENTION"
    assert by_id[attention.id]["failure_category"] == "ACTION"
