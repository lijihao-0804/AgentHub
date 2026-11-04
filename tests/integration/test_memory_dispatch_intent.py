"""PostgreSQL checks for committing Memory dispatch intent with a Run."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import Agent, AgentRun, AgentVersion
from packages.agent_runtime.runtime_service import AgentRunService
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.memory.models import MemoryExtractionJob
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads.models import AgentThread
from tests.support.ae6b_database_urls import require_isolated_test_database_url

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="PostgreSQL required"),
]

_CANDIDATE_PAYLOAD = [
    {
        "content": "release day is Friday",
        "kind": "PREFERENCE",
        "evidence": "Release day is Friday",
    }
]


@pytest_asyncio.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    guarded = require_isolated_test_database_url(TEST_DATABASE_URL).set(
        drivername="postgresql+asyncpg"
    )
    engine, factory = create_database(guarded.render_as_string(hide_password=False))
    try:
        # The task-owned database is migrated externally. This check also
        # prevents an unexpectedly old test database from silently passing.
        async with factory() as session:
            await session.execute(text("SELECT 1 FROM memory_extraction_jobs LIMIT 0"))
        yield factory
    finally:
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class _Base:
    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    context: WorkspaceExecutionContext


@dataclass(frozen=True, slots=True)
class _Run:
    run_id: UUID
    workspace_id: UUID
    thread_id: UUID | None
    created_by: UUID


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "request_id": request_id,
            }
        )


class _FailingQueue:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.calls = 0

    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        del workspace_id, run_id, request_id
        self.calls += 1
        raise ConnectionError(self.secret)


class _ProcessInterrupted(BaseException):
    pass


class _InterruptBeforeEnqueueService(AgentRunService):
    async def _enqueue_memory_extraction(self, context, run) -> None:
        del context, run
        raise _ProcessInterrupted


def _context(user_id: UUID, organization_id: UUID, workspace_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"memory-dispatch-{uuid4().hex}",
                trace_id=f"memory-dispatch-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run"}),
    )


async def _seed_base(factory: async_sessionmaker[AsyncSession]) -> _Base:
    async with factory() as session:
        user = User(
            email=f"memory-dispatch-{uuid4().hex}@example.test",
            normalized_email=f"memory-dispatch-{uuid4().hex}@example.test",
            password_hash="not-used",
        )
        user.normalized_email = user.email
        session.add(user)
        await session.flush()

        organization = Organization(name="Memory dispatch", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )

        workspace = Workspace(organization_id=organization.id, name="Memory dispatch")
        session.add(workspace)
        await session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role="DEVELOPER",
            )
        )

        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name="memory-dispatch-test",
            secret="not-used",
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="fake-model",
            temperature=Decimal("0"),
            max_tokens=128,
            timeout_seconds=Decimal("5"),
            capabilities={"tool_calling": True},
        )
        session.add(profile)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name="memory-dispatch-agent",
            system_prompt="test",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.flush()

        resolved = {"runtime": {}}
        version = AgentVersion(
            workspace_id=workspace.id,
            agent_id=agent.id,
            version_number=1,
            spec_schema_version=1,
            resolved_spec=resolved,
            resolved_spec_hash=canonical_json_hash(resolved),
            created_by=user.id,
        )
        session.add(version)
        await session.commit()

        return _Base(
            user_id=user.id,
            organization_id=organization.id,
            workspace_id=workspace.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            context=_context(user.id, organization.id, workspace.id),
        )


async def _new_run(
    factory: async_sessionmaker[AsyncSession],
    base: _Base,
    *,
    with_thread: bool = True,
) -> _Run:
    async with factory() as session:
        thread_id = None
        if with_thread:
            thread = AgentThread(
                workspace_id=base.workspace_id,
                agent_id=base.agent_id,
                title="memory dispatch",
                created_by=base.user_id,
            )
            session.add(thread)
            await session.flush()
            thread_id = thread.id
        run = AgentRun(
            workspace_id=base.workspace_id,
            agent_version_id=base.agent_version_id,
            thread_id=thread_id,
            input_text="Release day is Friday",
            created_by=base.user_id,
            status="RUNNING",
        )
        session.add(run)
        await session.commit()
        return _Run(
            run_id=run.id,
            workspace_id=run.workspace_id,
            thread_id=run.thread_id,
            created_by=run.created_by,
        )


async def _complete(
    service: AgentRunService,
    base: _Base,
    run: _Run,
    *,
    status: str = "SUCCEEDED",
):
    return await service._complete_run(
        base.context,
        run.run_id,
        status=status,
        final_output="answer" if status == "SUCCEEDED" else None,
        failure_code=None if status == "SUCCEEDED" else "SYNTHETIC_FAILURE",
        model_step_count=1,
        tool_call_count=0,
        usage_records=[],
    )


async def _job(factory: async_sessionmaker[AsyncSession], run: _Run) -> MemoryExtractionJob | None:
    async with factory() as session:
        job = await session.get(
            MemoryExtractionJob,
            {"workspace_id": run.workspace_id, "agent_run_id": run.run_id},
        )
        if job is not None:
            session.expunge(job)
        return job


@pytest.mark.asyncio
async def test_broker_failure_keeps_committed_pending_intent(
    db_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = await _seed_base(db_factory)
    run = await _new_run(db_factory, base)
    secret = "provider-content-must-not-be-logged"
    queue = _FailingQueue(secret)
    service = AgentRunService(db_factory, memory_writer=queue)

    with caplog.at_level(logging.WARNING, logger="packages.agent_runtime.runtime_service"):
        result = await _complete(service, base, run)

    assert result.status == "SUCCEEDED"
    assert queue.calls == 1
    async with db_factory() as session:
        parent = await session.get(AgentRun, run.run_id)
        job = await session.get(
            MemoryExtractionJob,
            {"workspace_id": run.workspace_id, "agent_run_id": run.run_id},
        )
    assert parent is not None and parent.status == "SUCCEEDED"
    assert parent.completed_at is not None
    assert job is not None and job.status == "PENDING"
    records = [
        record
        for record in caplog.records
        if record.name == "packages.agent_runtime.runtime_service"
        and record.message == "memory_extraction_enqueue_failed"
    ]
    assert len(records) == 1 and records[0].exc_info is None
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_process_interruption_after_commit_before_enqueue_keeps_intent(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_base(db_factory)
    run = await _new_run(db_factory, base)
    service = _InterruptBeforeEnqueueService(db_factory, memory_writer=_RecordingQueue())

    with pytest.raises(_ProcessInterrupted):
        await _complete(service, base, run)

    async with db_factory() as session:
        parent = await session.get(AgentRun, run.run_id)
        job = await session.get(
            MemoryExtractionJob,
            {"workspace_id": run.workspace_id, "agent_run_id": run.run_id},
        )
    assert parent is not None and parent.status == "SUCCEEDED"
    assert parent.completed_at is not None
    assert job is not None and job.status == "PENDING"


@pytest.mark.asyncio
async def test_repeated_completion_preserves_worker_state(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_base(db_factory)
    queue = _RecordingQueue()
    service = AgentRunService(db_factory, memory_writer=queue)

    for status, payload, failure_code in (
        ("RUNNING", _CANDIDATE_PAYLOAD, "MEMORY_EXTRACTION_RECORDING_PENDING"),
        ("FAILED", None, "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"),
    ):
        run = await _new_run(db_factory, base)
        await _complete(service, base, run)
        async with db_factory() as session:
            job = await session.get(
                MemoryExtractionJob,
                {"workspace_id": run.workspace_id, "agent_run_id": run.run_id},
            )
            assert job is not None
            job.status = status
            job.candidate_payload = payload
            job.failure_code = failure_code
            job.claimed_at = datetime.now(UTC) if status == "RUNNING" else None
            job.completed_at = None if status == "RUNNING" else datetime.now(UTC)
            await session.commit()

        await _complete(service, base, run)

        preserved = await _job(db_factory, run)
        assert preserved is not None
        assert preserved.status == status
        assert preserved.candidate_payload == payload
        assert preserved.failure_code == failure_code

    assert len(queue.calls) == 4


@pytest.mark.asyncio
async def test_ineligible_completion_does_not_create_intent(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed_base(db_factory)
    queue = _RecordingQueue()
    writer_service = AgentRunService(db_factory, memory_writer=queue)

    failed_run = await _new_run(db_factory, base)
    await _complete(writer_service, base, failed_run, status="FAILED")

    playground_run = await _new_run(db_factory, base, with_thread=False)
    await _complete(writer_service, base, playground_run)

    no_writer_run = await _new_run(db_factory, base)
    await _complete(AgentRunService(db_factory), base, no_writer_run)

    assert queue.calls == []
    assert await _job(db_factory, failed_run) is None
    assert await _job(db_factory, playground_run) is None
    assert await _job(db_factory, no_writer_run) is None
