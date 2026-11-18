"""PostgreSQL checks for the long-term-memory hardening contract."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import Agent
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.core.config.settings import get_settings
from packages.core.database import create_database
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
        content="团队项目每周五发布版本", kind="PREFERENCE", evidence="每周五发布版本"
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
            MemoryCandidate(
                content="项目生产环境采用蓝绿部署", kind="FACT", evidence="采用蓝绿部署"
            ),
            MemoryCandidate(
                content="项目发布前需要人工审批",
                kind="CONSTRAINT",
                evidence="发布前需要人工审批",
            ),
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
    candidate = MemoryCandidate(
        content="知识库数据按月进行归档", kind="FACT", evidence="按月进行归档"
    )
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


async def _wait_for_memory_insert_lock(db_factory) -> None:  # noqa: ANN001
    """Wait for PostgreSQL to prove writer 2 reached the unique index wait."""

    deadline = asyncio.get_running_loop().time() + 5
    statement = text(
        """
        SELECT count(*)
        FROM pg_stat_activity
        WHERE wait_event_type = 'Lock'
          AND query ILIKE '%workspace_memories%'
          AND query ILIKE '%INSERT%'
        """
    )
    while asyncio.get_running_loop().time() < deadline:
        async with db_factory() as session:
            waiting = int(await session.scalar(statement) or 0)
        if waiting:
            return
        # This is an event-loop yield, not a timing delay: the database lock
        # is the synchronization primitive and the deadline only bounds a
        # broken test setup.
        await asyncio.sleep(0)
    raise AssertionError("writer 2 never reached the PostgreSQL unique-index wait")


@pytest.mark.asyncio
async def test_postgres_collision_savepoint_preserves_siblings(db_factory) -> None:
    """A real insert collision must only roll back candidate A's savepoint."""

    workspace_id, agent_id = await _seed_workspace(db_factory)
    candidate_a = MemoryCandidate(
        content="项目发布前需要人工审批", kind="CONSTRAINT", evidence="需要人工审批"
    )
    candidate_b = MemoryCandidate(
        content="项目生产环境采用蓝绿部署", kind="FACT", evidence="采用蓝绿部署"
    )
    candidate_c = MemoryCandidate(
        content="知识库数据按月进行归档", kind="FACT", evidence="按月进行归档"
    )
    winner_engine, winner_factory = create_database(async_database_url(TEST_DATABASE_URL))
    writer_engine, writer_factory = create_database(async_database_url(TEST_DATABASE_URL))
    observer_engine, observer_factory = create_database(async_database_url(TEST_DATABASE_URL))
    writer_task: asyncio.Task[tuple[object, ...]] | None = None
    try:
        # Writer 1 holds A uncommitted. Writer 2's initial SELECT therefore
        # sees no A, but its INSERT is forced to wait on the real partial
        # unique index until Writer 1 commits.
        async with winner_factory() as winner_session:
            await winner_session.begin()
            winner_session.add(
                WorkspaceMemory(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    content=candidate_a.content,
                    content_hash=content_hash(candidate_a.content),
                    kind=candidate_a.kind,
                    status="ACTIVE",
                    provenance={"evidence": candidate_a.evidence},
                )
            )
            await winner_session.flush()

            store = SqlAlchemyMemoryStore(writer_factory)
            writer_task = asyncio.create_task(
                store.record(
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    thread_id=None,
                    source_run_id=None,
                    candidates=(candidate_a, candidate_b, candidate_c),
                )
            )
            await _wait_for_memory_insert_lock(observer_factory)
            await winner_session.commit()
            created = await writer_task

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
        by_content = {row.content: row for row in rows}
        assert len(rows) == 3
        assert sum(row.content == candidate_a.content for row in rows) == 1
        assert by_content[candidate_a.content].salience == 2
        assert by_content[candidate_b.content].id in set(created)
        assert by_content[candidate_c.content].id in set(created)
        assert set(created) == {
            by_content[candidate_b.content].id,
            by_content[candidate_c.content].id,
        }
    finally:
        if writer_task is not None and not writer_task.done():
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)
        await winner_engine.dispose()
        await writer_engine.dispose()
        await observer_engine.dispose()


@pytest.mark.asyncio
async def test_private_memory_isolation_replay_and_owner_cleanup(db_factory) -> None:
    from types import SimpleNamespace

    from packages.core.errors.exceptions import AgentHubError
    from packages.memory.service import MemoryAdminService

    workspace_id, agent_id = await _seed_workspace(db_factory)
    foreign_workspace, foreign_agent = await _seed_workspace(db_factory)
    store = SqlAlchemyMemoryStore(db_factory)
    admin = MemoryAdminService()
    owner, colleague = uuid4(), uuid4()
    candidate = MemoryCandidate(content="Release day is Friday", kind="FACT")
    record = dict(
        workspace_id=workspace_id,
        agent_id=agent_id,
        thread_id=None,
        source_run_id=None,
        candidates=(candidate,),
    )
    query = dict(workspace_id=workspace_id, agent_id=agent_id, query="release", limit=8)
    own, other, shared = [
        (await store.record(**record, owner_user_id=user))[0] for user in (owner, colleague, None)
    ]
    assert len({own, other, shared}) == 3
    for user, expected in ((owner, own), (colleague, other), (None, shared)):
        assert [item.id for item in await store.select(**query, owner_user_id=user)] == [expected]
    for keys in ({"agent_id": foreign_agent}, {"workspace_id": foreign_workspace}):
        assert not await store.select(**(query | keys), owner_user_id=owner)
    with pytest.raises(MemorySnapshotIntegrityError):
        await store.load(
            workspace_id=workspace_id,
            agent_id=agent_id,
            owner_user_id=colleague,
            memory_ids=(own,),
            memory_content_hashes={own: content_hash(candidate.content)},
        )
    context = SimpleNamespace(
        workspace_id=str(workspace_id), user_id=str(owner), permissions={"agent_run"}
    )
    async with db_factory() as session:
        page = await admin.list_for_agent(session, context, agent_id=agent_id)
        assert page.total == 2 and {row.id for row in page.items} == {own, shared}
        for memory_id, status in ((other, 404), (shared, 403)):
            with pytest.raises(AgentHubError) as denied:
                await admin.invalidate(session, context, agent_id=agent_id, memory_id=memory_id)
            assert denied.value.status_code == status
        await admin.invalidate(session, context, agent_id=agent_id, memory_id=own)
        assert not await store.select(**query, owner_user_id=owner)
        assert [
            item.id
            for item in await store.load(
                workspace_id=workspace_id,
                agent_id=agent_id,
                owner_user_id=owner,
                memory_ids=(own,),
            )
        ] == [own]
        await admin.reactivate(session, context, agent_id=agent_id, memory_id=own)
        assert [item.id for item in await store.select(**query, owner_user_id=owner)] == [own]
