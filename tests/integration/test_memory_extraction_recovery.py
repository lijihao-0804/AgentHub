"""Real PostgreSQL ownership, orphan recovery and post-response fencing."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from apps.worker.tasks import memories as worker
from packages.control_plane.models import OrganizationMembership, Workspace, WorkspaceMembership
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.memory.contracts import MemoryCandidate
from packages.memory.models import MemoryExtractionJob
from packages.model_gateway.gateway import SqlAlchemyModelGateway
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
    db_factory as db_factory,
)
from tests.integration.test_long_term_memory_hardening import (
    migrated_database as migrated_database,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]
PAYLOAD = (
    '{"memories":[{"content":"release day is Friday","kind":"PREFERENCE",'
    '"evidence":"Release day is Friday"}]}'
)


def settings():
    return Settings(_env_file=None, database_url=os.environ["AGENTHUB_TEST_DATABASE_URL"])


def gateway(monkeypatch, adapter):
    monkeypatch.setattr(
        worker,
        "SqlAlchemyModelGateway",
        lambda session, credential_cipher=None, trace_sink=None: SqlAlchemyModelGateway(
            session, adapter=adapter, credential_cipher=credential_cipher, trace_sink=trace_sink
        ),
    )


async def run_extraction(workspace, run):
    await worker._extract_run_memories(
        settings(), workspace_id=workspace, run_id=run.id, request_id=str(uuid4())
    )


async def stale_job(factory, workspace, run, payload):
    async with factory() as session:
        session.add(
            MemoryExtractionJob(
                workspace_id=workspace,
                agent_run_id=run.id,
                status="RUNNING",
                candidate_payload=payload,
                updated_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        await session.commit()


def candidates():
    return [
        asdict(
            MemoryCandidate(
                content="release day is Friday", kind="PREFERENCE", evidence="Release day is Friday"
            )
        )
    ]


@pytest.mark.asyncio
async def test_live_worker_owns_duplicate_delivery_and_stale_reconcile(db_factory, monkeypatch):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(_RecordingAdapter):
        async def complete(self, *args):
            entered.set()
            await release.wait()
            return await super().complete(*args)

    adapter = Blocking(PAYLOAD)
    gateway(monkeypatch, adapter)
    original = asyncio.create_task(run_extraction(workspace, run))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await run_extraction(workspace, run)
        queued = []

        async def enqueue(**kwargs):
            queued.append(kwargs)

        result = await worker._reconcile_memory_jobs(
            db_factory,
            workspace_id=workspace,
            enqueue=enqueue,
            now=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert result["active"] == 1 and result["unknown"] == 0 and not queued
        assert (await _job(db_factory, workspace, run.id)).status == "RUNNING"
    finally:
        release.set()
        await asyncio.wait_for(original, 10)
    assert adapter.calls == 1
    assert (await _job(db_factory, workspace, run.id)).status == "COMPLETED"
    assert len(await _memory_rows(db_factory, workspace, agent)) == 1


@pytest.mark.asyncio
async def test_orphans_recover_payload_without_provider_and_fence_uncertain_send(
    db_factory, monkeypatch
):
    workspace, agent = await _seed_workspace(db_factory)
    unknown, _ = await _seed_run(db_factory, workspace, agent)
    known, _ = await _seed_run(db_factory, workspace, agent)
    await stale_job(db_factory, workspace, unknown, None)
    await stale_job(db_factory, workspace, known, candidates())
    queued = []

    async def enqueue(**kwargs):
        queued.append(kwargs)

    now = datetime.now(UTC)
    results = await asyncio.gather(
        *(
            worker._reconcile_memory_jobs(
                db_factory, workspace_id=workspace, enqueue=enqueue, now=now
            )
            for _ in range(2)
        )
    )
    assert sum(r["unknown"] for r in results) == 1
    assert sum(r["enqueued"] for r in results) == 1 and len(queued) == 1
    assert queued[0]["run_id"] == known.id
    assert (
        await _job(db_factory, workspace, unknown.id)
    ).failure_code == "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)
    await run_extraction(workspace, known)
    await run_extraction(workspace, unknown)
    assert adapter.calls == 0
    rows = await _memory_rows(db_factory, workspace, agent)
    assert len(rows) == 1 and rows[0].salience == 1
    # Model the write-committed / job-completion-not-committed crash window.
    async with db_factory() as session:
        await session.execute(
            update(MemoryExtractionJob)
            .where(MemoryExtractionJob.agent_run_id == known.id)
            .values(
                status="RUNNING",
                updated_at=now - timedelta(minutes=10),
                failure_code="MEMORY_EXTRACTION_RECORDING_PENDING",
            )
        )
        await session.commit()
    await worker._reconcile_memory_jobs(
        db_factory, workspace_id=workspace, enqueue=enqueue, now=now
    )
    await run_extraction(workspace, known)
    assert adapter.calls == 0
    rows = await _memory_rows(db_factory, workspace, agent)
    assert len(rows) == 1 and rows[0].salience == 1
    job = await _job(db_factory, workspace, known.id)
    assert job.status == "COMPLETED" and job.failure_code is None


@pytest.mark.asyncio
async def test_enqueue_failure_remains_due_without_another_model_attempt(db_factory):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    await stale_job(db_factory, workspace, run, candidates())

    async def fail(**kwargs):
        raise ConnectionError("synthetic broker failure")

    now = datetime.now(UTC)
    result = await worker._reconcile_memory_jobs(
        db_factory, workspace_id=workspace, enqueue=fail, now=now
    )
    assert result["enqueue_failed"] == 1
    queued = []

    async def enqueue(**kwargs):
        queued.append(kwargs)

    assert (
        await worker._reconcile_memory_jobs(
            db_factory, workspace_id=workspace, enqueue=enqueue, now=now
        )
    )["enqueued"] == 0
    assert (
        await worker._reconcile_memory_jobs(
            db_factory, workspace_id=workspace, enqueue=enqueue, now=now + timedelta(minutes=6)
        )
    )["enqueued"] == 1
    job = await _job(db_factory, workspace, run.id)
    assert job.candidate_payload == candidates() and job.status == "RUNNING"


@pytest.mark.asyncio
async def test_late_provider_response_cannot_revive_terminal_unknown(db_factory, monkeypatch):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)

    class Late(_RecordingAdapter):
        async def complete(self, *args):
            async with db_factory() as session:
                await session.execute(
                    update(MemoryExtractionJob)
                    .where(MemoryExtractionJob.agent_run_id == run.id)
                    .values(status="FAILED", failure_code="MEMORY_EXTRACTION_OUTCOME_UNKNOWN")
                )
                await session.commit()
            return await super().complete(*args)

    adapter = Late(PAYLOAD)
    gateway(monkeypatch, adapter)
    await run_extraction(workspace, run)
    await worker._finish_extraction(
        db_factory, workspace_id=workspace, run_id=run.id, status="COMPLETED"
    )
    job = await _job(db_factory, workspace, run.id)
    assert adapter.calls == 1 and job.status == "FAILED" and job.candidate_payload is None
    assert not await _memory_rows(db_factory, workspace, agent)


@pytest.mark.asyncio
async def test_membership_revoked_during_provider_call_blocks_memory_write(db_factory, monkeypatch):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)

    class Revoked(_RecordingAdapter):
        async def complete(self, *args):
            async with db_factory() as session:
                organization = await session.scalar(
                    select(Workspace.organization_id).where(Workspace.id == workspace)
                )
                await session.execute(
                    update(OrganizationMembership)
                    .where(OrganizationMembership.organization_id == organization)
                    .values(role="MEMBER")
                )
                await session.execute(
                    update(WorkspaceMembership)
                    .where(WorkspaceMembership.workspace_id == workspace)
                    .values(role="VIEWER")
                )
                await session.commit()
            return await super().complete(*args)

    adapter = Revoked(PAYLOAD)
    gateway(monkeypatch, adapter)
    await run_extraction(workspace, run)
    job = await _job(db_factory, workspace, run.id)
    assert (
        adapter.calls == 1 and job.status == "SKIPPED" and job.failure_code == "AGENT_RUN_FORBIDDEN"
    )
    assert not await _memory_rows(db_factory, workspace, agent)


@pytest.mark.asyncio
async def test_context_admission_failure_prevents_memory_model_dispatch(db_factory, monkeypatch):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)

    def refuse(*args, **kwargs):
        raise AgentHubError("AGENT_CONTEXT_BUDGET_EXCEEDED", "synthetic refusal")

    monkeypatch.setattr(worker.ContextBudgetPolicy, "admit", refuse)
    await run_extraction(workspace, run)
    assert adapter.calls == 0
    assert (await _job(db_factory, workspace, run.id)).status == "FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [1, 2, 3], ids=["initial", "before-send", "before-write"])
@pytest.mark.parametrize("status_code", [403, 404])
async def test_revoked_membership_has_one_skip_semantic(
    db_factory, monkeypatch, phase, status_code
):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)
    original = worker.TenantService.get_workspace_access
    authorizations = 0

    async def authorize(self, *args, **kwargs):
        nonlocal authorizations
        authorizations += 1
        if authorizations == phase:
            raise AgentHubError("ACCESS_REVOKED", "Synthetic access revocation.", status_code)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(worker.TenantService, "get_workspace_access", authorize)
    await run_extraction(workspace, run)
    job = await _job(db_factory, workspace, run.id)
    assert job.status == "SKIPPED"
    assert job.failure_code == "AGENT_RUN_FORBIDDEN"
    assert adapter.calls == (1 if phase == 3 else 0)
    assert not await _memory_rows(db_factory, workspace, agent)


@pytest.mark.asyncio
async def test_unsent_pending_intent_survives_broker_failure_then_extracts_once(
    db_factory, monkeypatch
):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    now = datetime.now(UTC)
    async with db_factory() as session:
        session.add(
            MemoryExtractionJob(
                workspace_id=workspace,
                agent_run_id=run.id,
                status="PENDING",
                updated_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()
    adapter = _RecordingAdapter(PAYLOAD)
    gateway(monkeypatch, adapter)

    async def unavailable(**kwargs):
        raise RuntimeError("Synthetic broker refusal.")

    failed = await worker._reconcile_memory_jobs(
        db_factory, enqueue=unavailable, now=now, workspace_id=workspace
    )
    assert failed["enqueue_failed"] == 1 and failed["unknown"] == 0
    assert (await _job(db_factory, workspace, run.id)).status == "PENDING"
    assert adapter.calls == 0
    queued = []

    async def enqueue(**kwargs):
        queued.append(kwargs)

    too_soon = await worker._reconcile_memory_jobs(
        db_factory, enqueue=enqueue, now=now, workspace_id=workspace
    )
    assert too_soon["enqueued"] == 0
    recovered = await worker._reconcile_memory_jobs(
        db_factory,
        enqueue=enqueue,
        now=now + worker.MEMORY_RECONCILE_STALE_AFTER + timedelta(seconds=1),
        workspace_id=workspace,
    )
    assert recovered["enqueued"] == 1 and len(queued) == 1
    assert (await _job(db_factory, workspace, run.id)).status == "PENDING"
    await run_extraction(workspace, run)
    await run_extraction(workspace, run)
    assert adapter.calls == 1
    job = await _job(db_factory, workspace, run.id)
    assert job.status == "COMPLETED" and job.failure_code is None
    rows = await _memory_rows(db_factory, workspace, agent)
    assert len(rows) == 1 and rows[0].salience == 1


@pytest.mark.asyncio
async def test_lost_owner_connection_never_replays_or_records_a_late_response(
    db_factory, monkeypatch
):
    workspace, agent = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace, agent)
    entered, release = asyncio.Event(), asyncio.Event()
    owner_pids = []
    original_lock = worker._try_own_extraction

    async def capture_owner(session, *args):
        owned = await original_lock(session, *args)
        if owned:
            owner_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        return owned

    class Blocked(_RecordingAdapter):
        async def complete(self, *args):
            entered.set()
            await release.wait()
            return await super().complete(*args)

    adapter = Blocked(PAYLOAD)
    gateway(monkeypatch, adapter)
    monkeypatch.setattr(worker, "_try_own_extraction", capture_owner)
    original = asyncio.create_task(run_extraction(workspace, run))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with db_factory() as session:
            # This is the connection created by this test's extraction owner,
            # never an arbitrary worker or another database's backend.
            assert owner_pids
            assert await session.scalar(text("SELECT current_database()")) != "postgres"
            terminated = await session.scalar(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": owner_pids[0]}
            )
            assert terminated
        await run_extraction(workspace, run)
        assert (
            await _job(db_factory, workspace, run.id)
        ).failure_code == "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
    finally:
        release.set()
        try:
            await asyncio.wait_for(original, 10)
        except DBAPIError:
            pass  # The original ownership transaction cannot commit a lost connection.
    assert adapter.calls == 1
    job = await _job(db_factory, workspace, run.id)
    assert job.status == "FAILED" and job.candidate_payload is None
    assert not await _memory_rows(db_factory, workspace, agent)
