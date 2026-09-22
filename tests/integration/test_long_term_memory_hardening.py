"""PostgreSQL checks for the long-term-memory hardening contract."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.agent_runtime.models import Agent
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.memory.contracts import MemoryCandidate, MemorySnapshotIntegrityError
from packages.memory.models import WorkspaceMemory
from packages.memory.store import SqlAlchemyMemoryStore, content_hash
from packages.model_gateway.models import ModelProfile, ProviderCredential

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run PostgreSQL memory integration tests.",
    ),
]


async def _seed_workspace(db_factory) -> tuple[object, object]:  # noqa: ANN001
    async with db_factory() as session:
        user = User(
            email=f"memory-{uuid4().hex}@example.test",
            normalized_email=f"memory-{uuid4().hex}@example.test",
            password_hash="not-used",
        )
        # Keep the unique normalized email identical to the visible email.
        user.normalized_email = user.email
        session.add(user)
        await session.flush()
        organization = Organization(name="Memory hardening", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id, user_id=user.id, role="OWNER"
            )
        )
        workspace = Workspace(organization_id=organization.id, name="Memory hardening")
        session.add(workspace)
        await session.flush()
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name="memory-test-credential",
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
            name="memory-agent",
            system_prompt="test",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.commit()
        return workspace.id, agent.id


@pytest.mark.asyncio
async def test_postgres_candidate_conflict_does_not_rollback_siblings(db_factory) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    store = SqlAlchemyMemoryStore(db_factory)
    repeated = MemoryCandidate(
        content="团队每周五发版", kind="PREFERENCE", evidence="每周五发版"
    )
    await store.record(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=None,
        candidates=(repeated,),
    )

    created = await store.record(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=None,
        candidates=(
            repeated,
            MemoryCandidate(content="项目使用蓝绿部署", kind="FACT", evidence="使用蓝绿部署"),
            MemoryCandidate(content="发布前需要审批", kind="CONSTRAINT", evidence="发布前需要审批"),
        ),
    )

    assert len(created) == 2
    async with db_factory() as session:
        rows = (
            await session.scalars(
                select(WorkspaceMemory).where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.agent_id == agent_id,
                    WorkspaceMemory.status == "ACTIVE",
                )
            )
        ).all()
    assert len(rows) == 3
    by_content = {row.content: row for row in rows}
    assert by_content[repeated.content].salience == 2
    assert {row.id for row in rows if row.content != repeated.content} == set(created)


@pytest.mark.asyncio
async def test_postgres_partial_unique_index_handles_independent_writers(db_factory) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    candidate = MemoryCandidate(content="项目采用灰度发布", kind="FACT", evidence="采用灰度发布")
    store = SqlAlchemyMemoryStore(db_factory)

    results = await asyncio.gather(
        store.record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            thread_id=None,
            source_run_id=None,
            candidates=(candidate,),
        ),
        store.record(
            workspace_id=workspace_id,
            agent_id=agent_id,
            thread_id=None,
            source_run_id=None,
            candidates=(candidate,),
        ),
    )

    assert sum(len(result) for result in results) <= 1
    async with db_factory() as session:
        rows = (
            await session.scalars(
                select(WorkspaceMemory).where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.agent_id == agent_id,
                    WorkspaceMemory.content_hash == content_hash(candidate.content),
                )
            )
        ).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_postgres_snapshot_hash_match_mismatch_and_missing_id(db_factory) -> None:
    workspace_id, agent_id = await _seed_workspace(db_factory)
    store = SqlAlchemyMemoryStore(db_factory)
    candidate = MemoryCandidate(content="知识库按月归档", kind="FACT", evidence="按月归档")
    await store.record(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=None,
        candidates=(candidate,),
    )
    selected = await store.select(
        workspace_id=workspace_id, agent_id=agent_id, query="归档", limit=1
    )
    item = selected[0]

    loaded = await store.load(
        workspace_id=workspace_id,
        memory_ids=(item.id,),
        memory_content_hashes={item.id: item.content_hash},
    )
    assert loaded[0].content_hash == item.content_hash

    with pytest.raises(MemorySnapshotIntegrityError):
        await store.load(
            workspace_id=workspace_id,
            memory_ids=(item.id,),
            memory_content_hashes={item.id: "0" * 64},
        )
    missing_id = uuid4()
    with pytest.raises(MemorySnapshotIntegrityError):
        await store.load(
            workspace_id=workspace_id,
            memory_ids=(missing_id,),
            memory_content_hashes={missing_id: "0" * 64},
        )
