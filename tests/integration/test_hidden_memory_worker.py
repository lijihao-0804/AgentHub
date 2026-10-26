"""Source-run memory idempotency and worker redelivery authorization."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from apps.worker.tasks import memories as worker
from packages.agent_runtime.models import Agent, AgentRun, AgentVersion
from packages.control_plane.models import (
    OrganizationMembership,
    Workspace,
    WorkspaceMembership,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.memory.contracts import MemoryCandidate
from packages.memory.models import MemoryContribution, MemoryExtractionJob, WorkspaceMemory
from packages.memory.store import SqlAlchemyMemoryStore
from packages.model_gateway.contracts import ModelResponse, ModelUsage
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads.models import AgentThread
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


class _RecordingAdapter:
    def __init__(self, payload: str = '{"memories": []}') -> None:
        self.payload = payload
        self.calls = 0

    async def complete(self, profile, credential, request):
        del request
        self.calls += 1
        return ModelResponse(
            content=self.payload,
            provider=credential.provider,
            model=profile.model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


async def _seed_run(
    factory,
    workspace_id: UUID,
    agent_id: UUID,
    *,
    input_text: str = "Release day is Friday",
) -> tuple[AgentRun, UUID]:
    async with factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        user_id = await session.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == workspace.organization_id,
                OrganizationMembership.role == "OWNER",
            )
        )
        assert user_id is not None
        if await session.get(WorkspaceMembership, (workspace_id, user_id)) is None:
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role="DEVELOPER",
                )
            )
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        profile = await session.get(ModelProfile, agent.model_profile_id)
        assert profile is not None
        credential = await session.get(ProviderCredential, profile.provider_credential_id)
        assert credential is not None
        resolved = {
            "spec_schema_version": 1,
            "model": {
                "profile_id": str(profile.id),
                "credential_ref": str(profile.provider_credential_id),
                "provider": credential.provider,
                "model": profile.model,
                "temperature": str(profile.temperature),
                "max_tokens": profile.max_tokens,
                "timeout_seconds": str(profile.timeout_seconds),
                "capabilities": {
                    **profile.capabilities,
                    "max_context_tokens": 8192,
                },
                "retry_policy": {"max_attempts": 1},
                "fallback_chain": [],
                "fallback_profiles": [],
            },
            "prompt": {"system_prompt": "test", "prompt_version": 1},
            "retrieval": {"knowledge_snapshots": []},
            "tools": [],
            "runtime": {"memory": {"long_term_memory": True}},
        }
        version = await session.scalar(
            select(AgentVersion)
            .where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.agent_id == agent_id,
            )
            .order_by(AgentVersion.version_number.desc())
            .limit(1)
        )
        if version is None:
            version = AgentVersion(
                workspace_id=workspace_id,
                agent_id=agent_id,
                version_number=2,
                spec_schema_version=1,
                resolved_spec=resolved,
                resolved_spec_hash=canonical_json_hash(resolved),
                created_by=user_id,
            )
            session.add(version)
            await session.flush()
        thread = AgentThread(
            workspace_id=workspace_id,
            agent_id=agent_id,
            title="memory extraction",
            created_by=user_id,
        )
        session.add(thread)
        await session.flush()
        run = AgentRun(
            workspace_id=workspace_id,
            agent_version_id=version.id,
            thread_id=thread.id,
            input_text=input_text,
            final_output="noted",
            created_by=user_id,
            status="SUCCEEDED",
        )
        session.add(run)
        await session.commit()
        return run, thread.id


async def _job(factory, workspace_id: UUID, run_id: UUID) -> MemoryExtractionJob:
    async with factory() as session:
        job = await session.get(
            MemoryExtractionJob, {"workspace_id": workspace_id, "agent_run_id": run_id}
        )
        assert job is not None
        session.expunge(job)
        return job


async def _memory_rows(factory, workspace_id: UUID, agent_id: UUID) -> list[WorkspaceMemory]:
    async with factory() as session:
        return list(
            await session.scalars(
                select(WorkspaceMemory).where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.agent_id == agent_id,
                )
            )
        )


@pytest.mark.asyncio
async def test_source_run_contribution_is_idempotent_and_new_run_reinforces(
    db_factory,
) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace_id, agent_id)
    other, _ = await _seed_run(db_factory, workspace_id, agent_id)
    candidate = MemoryCandidate(
        content="release day is Friday",
        kind="PREFERENCE",
        evidence="Release day is Friday",
    )
    store = SqlAlchemyMemoryStore(db_factory)

    await store.record(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=run.id,
        candidates=(candidate,),
    )
    await store.record(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=run.id,
        candidates=(candidate,),
    )
    rows = await _memory_rows(db_factory, workspace_id, agent_id)
    assert len(rows) == 1 and rows[0].salience == 1
    async with db_factory() as session:
        claims = list(
            await session.scalars(
                select(MemoryContribution).where(
                    MemoryContribution.workspace_id == workspace_id,
                    MemoryContribution.agent_run_id == run.id,
                )
            )
        )
    assert len(claims) == 1

    await store.record(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=other.id,
        candidates=(candidate,),
    )
    rows = await _memory_rows(db_factory, workspace_id, agent_id)
    assert len(rows) == 1 and rows[0].salience == 2


@pytest.mark.asyncio
async def test_concurrent_same_run_replay_contributes_once(db_factory) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace_id, agent_id)
    candidate = MemoryCandidate(
        content="production changes require approval",
        kind="CONSTRAINT",
        evidence="Release day is Friday",
    )
    store = SqlAlchemyMemoryStore(db_factory)

    await asyncio.gather(
        store.record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            thread_id=None,
            source_run_id=run.id,
            candidates=(candidate,),
        ),
        store.record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            thread_id=None,
            source_run_id=run.id,
            candidates=(candidate,),
        ),
    )
    rows = await _memory_rows(db_factory, workspace_id, agent_id)
    assert len(rows) == 1 and rows[0].salience == 1
    async with db_factory() as session:
        claims = list(
            await session.scalars(
                select(MemoryContribution).where(
                    MemoryContribution.workspace_id == workspace_id,
                    MemoryContribution.agent_run_id == run.id,
                )
            )
        )
    assert len(claims) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [
        ("COMPLETED", None),
        ("SKIPPED", None),
        ("RUNNING", None),
    ],
)
async def test_terminal_or_uncertain_job_never_sends_again(
    db_factory, monkeypatch, status: str, payload: list[dict[str, str]] | None
) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace_id, agent_id)
    async with db_factory() as session:
        session.add(
            MemoryExtractionJob(
                workspace_id=workspace_id,
                agent_run_id=run.id,
                status=status,
                candidate_payload=payload,
            )
        )
        await session.commit()

    adapter = _RecordingAdapter()
    monkeypatch.setattr(
        worker,
        "SqlAlchemyModelGateway",
        lambda session, credential_cipher=None, trace_sink=None: SqlAlchemyModelGateway(
            session,
            adapter=adapter,
            credential_cipher=credential_cipher,
            trace_sink=trace_sink,
        ),
    )
    await worker._extract_run_memories(
        Settings(_env_file=None, database_url=os.environ["AGENTHUB_TEST_DATABASE_URL"]),
        workspace_id=workspace_id,
        run_id=run.id,
        request_id=f"memory-{uuid4().hex}",
    )

    assert adapter.calls == 0
    job = await _job(db_factory, workspace_id, run.id)
    if status == "RUNNING":
        assert job.status == "FAILED"
        assert job.failure_code == "MEMORY_EXTRACTION_OUTCOME_UNKNOWN"
    else:
        assert job.status == status


@pytest.mark.asyncio
async def test_running_job_with_candidates_records_without_model_call(
    db_factory, monkeypatch
) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace_id, agent_id)
    candidate = MemoryCandidate(
        content="release day is Friday",
        kind="PREFERENCE",
        evidence="Release day is Friday",
    )
    async with db_factory() as session:
        session.add(
            MemoryExtractionJob(
                workspace_id=workspace_id,
                agent_run_id=run.id,
                status="RUNNING",
                candidate_payload=[asdict(candidate)],
            )
        )
        await session.commit()
    adapter = _RecordingAdapter()
    monkeypatch.setattr(
        worker,
        "SqlAlchemyModelGateway",
        lambda session, credential_cipher=None, trace_sink=None: SqlAlchemyModelGateway(
            session,
            adapter=adapter,
            credential_cipher=credential_cipher,
            trace_sink=trace_sink,
        ),
    )

    await worker._extract_run_memories(
        Settings(_env_file=None, database_url=os.environ["AGENTHUB_TEST_DATABASE_URL"]),
        workspace_id=workspace_id,
        run_id=run.id,
        request_id=f"memory-{uuid4().hex}",
    )

    assert adapter.calls == 0
    rows = await _memory_rows(db_factory, workspace_id, agent_id)
    assert len(rows) == 1 and rows[0].salience == 1
    assert (await _job(db_factory, workspace_id, run.id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_new_job_sends_once_and_redelivery_never_sends_again(db_factory, monkeypatch) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace_id, agent_id)
    adapter = _RecordingAdapter(
        '{"memories": [{"content": "release day is Friday", '
        '"kind": "PREFERENCE", "evidence": "Release day is Friday"}]}'
    )
    monkeypatch.setattr(
        worker,
        "SqlAlchemyModelGateway",
        lambda session, credential_cipher=None, trace_sink=None: SqlAlchemyModelGateway(
            session,
            adapter=adapter,
            credential_cipher=credential_cipher,
            trace_sink=trace_sink,
        ),
    )
    settings = Settings(_env_file=None, database_url=os.environ["AGENTHUB_TEST_DATABASE_URL"])

    await worker._extract_run_memories(
        settings,
        workspace_id=workspace_id,
        run_id=run.id,
        request_id=f"memory-{uuid4().hex}",
    )
    await worker._extract_run_memories(
        settings,
        workspace_id=workspace_id,
        run_id=run.id,
        request_id=f"memory-{uuid4().hex}",
    )

    assert adapter.calls == 1
    rows = await _memory_rows(db_factory, workspace_id, agent_id)
    assert len(rows) == 1 and rows[0].salience == 1
    assert (await _job(db_factory, workspace_id, run.id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_role_downgrade_immediately_before_send_blocks_model(db_factory, monkeypatch) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    run, _ = await _seed_run(db_factory, workspace_id, agent_id)
    async with db_factory() as session:
        user_id = await session.scalar(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == workspace_id
            )
        )
        assert user_id is not None
        organization_id = await session.scalar(
            select(Workspace.organization_id).where(Workspace.id == workspace_id)
        )
        assert organization_id is not None
        await session.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
            )
            .values(role="MEMBER")
        )
        membership = await session.get(WorkspaceMembership, (workspace_id, user_id))
        assert membership is not None
        membership.role = "DEVELOPER"
        await session.commit()

    adapter = _RecordingAdapter()
    monkeypatch.setattr(
        worker,
        "SqlAlchemyModelGateway",
        lambda session, credential_cipher=None, trace_sink=None: SqlAlchemyModelGateway(
            session,
            adapter=adapter,
            credential_cipher=credential_cipher,
            trace_sink=trace_sink,
        ),
    )
    original_access = worker.TenantService.get_workspace_access
    calls = 0

    async def downgrade_before_send(service, session, *, principal, workspace_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            async with db_factory() as mutation:
                await mutation.execute(
                    update(WorkspaceMembership)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.user_id == UUID(str(principal.user_id)),
                    )
                    .values(role="VIEWER")
                )
                await mutation.commit()
        return await original_access(
            service, session, principal=principal, workspace_id=workspace_id
        )

    monkeypatch.setattr(worker.TenantService, "get_workspace_access", downgrade_before_send)
    await worker._extract_run_memories(
        Settings(_env_file=None, database_url=os.environ["AGENTHUB_TEST_DATABASE_URL"]),
        workspace_id=workspace_id,
        run_id=run.id,
        request_id=f"memory-{uuid4().hex}",
    )

    assert calls == 2
    assert adapter.calls == 0
    job = await _job(db_factory, workspace_id, run.id)
    assert job.status == "SKIPPED"
    assert job.failure_code == "AGENT_RUN_FORBIDDEN"
    assert await _memory_rows(db_factory, workspace_id, agent_id) == []
