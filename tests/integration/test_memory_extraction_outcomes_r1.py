"""R1/M01 memory extraction outcome classification and deterministic recovery."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.tasks import memories as worker
from packages.agent_runtime.models import AgentRun
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.models import ProviderCredential
from tests.integration.test_hidden_memory_worker import (
    _job,
    _memory_rows,
    _RecordingAdapter,
    _seed_run,
)
from tests.integration.test_long_term_memory_hardening import (
    _seed_workspace,
)
from tests.integration.test_long_term_memory_hardening import (
    db_factory as _upstream_db_factory,
)
from tests.integration.test_long_term_memory_hardening import (
    migrated_database as _upstream_migrated_database,
)
from tests.integration.test_memory_extraction_recovery import (
    PAYLOAD,
    candidates,
    gateway,
    run_extraction,
)
from tests.support.ae6b_database_urls import require_isolated_test_database_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]

PRIVATE_SENTINEL = "R1-M01-PRIVATE-CONTENT-MUST-NOT-BE-LOGGED"
INVALID_JSON_PAYLOAD = '{"private":"' + PRIVATE_SENTINEL + '" trailing}'


@pytest.fixture(scope="session")
def isolated_test_database_url() -> str:
    database_url = os.environ.get("AGENTHUB_TEST_DATABASE_URL")
    require_isolated_test_database_url(database_url)
    assert database_url is not None
    return database_url.strip()


@pytest.fixture(scope="session")
def migrated_database(isolated_test_database_url: str):
    del isolated_test_database_url
    yield from _upstream_migrated_database.__wrapped__()


@pytest_asyncio.fixture
async def db_factory(
    isolated_test_database_url: str,
    migrated_database: None,
) -> async_sessionmaker[AsyncSession]:
    del isolated_test_database_url
    async for factory in _upstream_db_factory.__wrapped__(migrated_database):
        yield factory


async def _parent_state(factory, run_id):
    async with factory() as session:
        parent = await session.get(AgentRun, run_id)
        assert parent is not None
        return parent.status, parent.final_output


def _assert_private_logs_clean(caplog):
    assert PRIVATE_SENTINEL not in caplog.text


async def _reconcile_future(factory, workspace_id):
    queued = []

    async def enqueue(**kwargs):
        queued.append(kwargs)

    result = await worker._reconcile_memory_jobs(
        factory,
        enqueue=enqueue,
        now=datetime.now(UTC) + worker.MEMORY_RECONCILE_STALE_AFTER + timedelta(seconds=1),
        workspace_id=workspace_id,
    )
    return result, queued


@contextmanager
def _fail_candidate_payload_update_once(factory, workspace_id, run_id):
    database = factory.kw["bind"].url.database
    state = {"injected": False}

    def fail_once(_connection, _cursor, statement, parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        rendered_parameters = repr(parameters)
        if (
            not state["injected"]
            and _connection.engine.url.database == database
            and normalized.startswith("update memory_extraction_jobs")
            and "candidate_payload" in normalized
            and str(workspace_id) in rendered_parameters
            and str(run_id) in rendered_parameters
        ):
            state["injected"] = True
            raise OperationalError(
                statement,
                parameters,
                ConnectionError(PRIVATE_SENTINEL),
            )

    # The worker creates its own engine. Restrict the class-level listener to
    # this isolated DB and exact source identity, then remove it unconditionally.
    event.listen(Engine, "before_cursor_execute", fail_once)
    try:
        yield state
    finally:
        event.remove(Engine, "before_cursor_execute", fail_once)


@pytest.mark.asyncio
async def test_context_budget_failure_is_known_non_dispatch(db_factory, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)

    def refuse(*_args, **_kwargs):
        raise AgentHubError("AGENT_CONTEXT_BUDGET_EXCEEDED", PRIVATE_SENTINEL)

    monkeypatch.setattr(worker.ContextBudgetPolicy, "admit", refuse)
    await run_extraction(workspace, run)

    job = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 0
    assert job.status == "FAILED"
    assert job.failure_code == "MEMORY_EXTRACTION_FAILED"
    assert job.candidate_payload is None
    assert await _memory_rows(db_factory, workspace, agent) == []

    await run_extraction(workspace, run)
    _result, queued = await _reconcile_future(db_factory, workspace)
    assert adapter.calls == 0 and queued == []
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
async def test_prepare_resolved_failure_is_known_non_dispatch(db_factory, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)
    async with db_factory() as session:
        disabled = await session.execute(
            update(ProviderCredential)
            .where(ProviderCredential.workspace_id == workspace)
            .values(enabled=False)
        )
        assert disabled.rowcount == 1
        await session.commit()

    await run_extraction(workspace, run)

    job = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 0
    assert job.status == "FAILED"
    assert job.failure_code == "MEMORY_EXTRACTION_FAILED"
    assert job.candidate_payload is None
    assert await _memory_rows(db_factory, workspace, agent) == []

    await run_extraction(workspace, run)
    _result, queued = await _reconcile_future(db_factory, workspace)
    assert adapter.calls == 0 and queued == []
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionError])
async def test_call_without_complete_response_is_unknown_and_never_resent(
    db_factory, monkeypatch, caplog, error_type
):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)

    class FailsAfterCall(_RecordingAdapter):
        async def complete(self, *_args):
            self.calls += 1
            raise error_type(PRIVATE_SENTINEL)

    adapter = FailsAfterCall()
    gateway(monkeypatch, adapter)
    await run_extraction(workspace, run)

    job = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1
    assert job.status == "FAILED"
    assert job.failure_code == "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
    assert job.candidate_payload is None
    assert await _memory_rows(db_factory, workspace, agent) == []

    await run_extraction(workspace, run)
    _result, queued = await _reconcile_future(db_factory, workspace)
    assert adapter.calls == 1 and queued == []
    assert (await _job(db_factory, workspace, run.id)).failure_code == (
        "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
    )
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
async def test_complete_response_parse_failure_is_response_processing_failure(
    db_factory, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)

    class ReturnsResponse(_RecordingAdapter):
        def __init__(self):
            super().__init__(PAYLOAD)
            self.responses_returned = 0

        async def complete(self, *args):
            response = await super().complete(*args)
            self.responses_returned += 1
            return response

    adapter = ReturnsResponse()
    gateway(monkeypatch, adapter)

    def fail_parse(*_args, **_kwargs):
        raise ValueError(PRIVATE_SENTINEL)

    monkeypatch.setattr(worker, "parse_candidates", fail_parse)
    await run_extraction(workspace, run)

    job = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1 and adapter.responses_returned == 1
    assert job.status == "FAILED"
    assert job.failure_code == "MEMORY_EXTRACTION_RESPONSE_PROCESSING_FAILED"
    assert job.candidate_payload is None
    assert await _memory_rows(db_factory, workspace, agent) == []

    await run_extraction(workspace, run)
    _result, queued = await _reconcile_future(db_factory, workspace)
    assert adapter.calls == 1 and queued == []
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
async def test_candidate_payload_save_failure_is_response_persistence_failure(
    db_factory, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)

    with _fail_candidate_payload_update_once(db_factory, workspace, run.id) as injection:
        await run_extraction(workspace, run)

    job = await _job(db_factory, workspace, run.id)
    assert injection["injected"] is True
    assert adapter.calls == 1
    assert job.status == "FAILED"
    assert job.failure_code == "MEMORY_EXTRACTION_RESPONSE_PERSISTENCE_FAILED"
    assert job.candidate_payload is None
    assert await _memory_rows(db_factory, workspace, agent) == []

    await run_extraction(workspace, run)
    _result, queued = await _reconcile_future(db_factory, workspace)
    assert adapter.calls == 1 and queued == []
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
async def test_persisted_candidates_recover_recording_without_model_or_duplicate_memory(
    db_factory, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)
    original_record = worker.SqlAlchemyMemoryStore.record
    record_calls = 0

    async def fail_recording_once(store, **kwargs):
        nonlocal record_calls
        record_calls += 1
        if record_calls == 1:
            raise RuntimeError(PRIVATE_SENTINEL)
        return await original_record(store, **kwargs)

    monkeypatch.setattr(worker.SqlAlchemyMemoryStore, "record", fail_recording_once)
    await run_extraction(workspace, run)

    pending = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1 and record_calls == 1
    assert pending.status == "RUNNING"
    assert pending.failure_code == "MEMORY_EXTRACTION_RECORDING_PENDING"
    assert pending.candidate_payload == candidates()
    assert await _memory_rows(db_factory, workspace, agent) == []

    result, queued = await _reconcile_future(db_factory, workspace)
    assert result["enqueued"] == 1 and len(queued) == 1
    assert queued[0]["workspace_id"] == workspace and queued[0]["run_id"] == run.id
    await run_extraction(workspace, run)
    await run_extraction(workspace, run)

    completed = await _job(db_factory, workspace, run.id)
    rows = await _memory_rows(db_factory, workspace, agent)
    assert adapter.calls == 1 and record_calls == 2
    assert completed.status == "COMPLETED" and completed.failure_code is None
    assert len(rows) == 1 and rows[0].salience == 1
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [INVALID_JSON_PAYLOAD, '{"memories": []}'],
    ids=["invalid-json", "empty-candidates"],
)
async def test_unusable_or_empty_response_still_completes(db_factory, monkeypatch, caplog, payload):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(payload)
    gateway(monkeypatch, adapter)

    await run_extraction(workspace, run)

    job = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1
    assert job.status == "COMPLETED" and job.failure_code is None
    assert job.candidate_payload == []
    assert await _memory_rows(db_factory, workspace, agent) == []
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)


@pytest.mark.asyncio
async def test_cancellation_propagates_then_reconcile_marks_unknown_without_resend(
    db_factory, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    entered = asyncio.Event()

    class WaitsInsideProvider(_RecordingAdapter):
        async def complete(self, *_args):
            self.calls += 1
            entered.set()
            await asyncio.Future()

    adapter = WaitsInsideProvider()
    gateway(monkeypatch, adapter)
    extraction = asyncio.create_task(run_extraction(workspace, run))
    await asyncio.wait_for(entered.wait(), timeout=10)
    extraction.cancel(PRIVATE_SENTINEL)
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await asyncio.wait_for(extraction, timeout=10)
    assert cancelled.value.args == (PRIVATE_SENTINEL,)

    running = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1
    assert running.status == "RUNNING" and running.candidate_payload is None

    result, queued = await _reconcile_future(db_factory, workspace)
    assert result["unknown"] == 1 and queued == []
    await run_extraction(workspace, run)

    unknown = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1
    assert unknown.status == "FAILED"
    assert unknown.failure_code == "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
    assert unknown.candidate_payload is None
    assert await _memory_rows(db_factory, workspace, agent) == []
    assert await _parent_state(db_factory, run.id) == ("SUCCEEDED", "noted")
    _assert_private_logs_clean(caplog)
