from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.core.auth.security import issue_access_token
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshotItem,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.snapshots import KnowledgeSnapshotService

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M3-F PostgreSQL integration tests.",
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
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous


@pytest_asyncio.fixture
async def db_factory(migrated_database: None) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class _Bundle:
    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    document_id: UUID
    revision_id: UUID


async def _seed_bundle(factory: async_sessionmaker[AsyncSession]) -> _Bundle:
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@m3f.test",
            normalized_email=f"{uuid4()}@m3f.test",
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"m3f-{uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )
        workspace = Workspace(organization_id=organization.id, name=f"workspace-{uuid4()}")
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Snapshot KB")
        session.add(knowledge_base)
        await session.flush()
        document = Document(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            name="source.txt",
        )
        session.add(document)
        await session.flush()
        revision = DocumentRevision(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            revision_number=1,
            original_filename="source.txt",
            blob_key=f"m3f/{uuid4().hex}",
            media_type="text/plain",
            file_size=10,
            ingestion_status=RevisionIngestionStatus.READY,
            lifecycle_status=RevisionLifecycleStatus.ACTIVE,
        )
        session.add(revision)
        await session.commit()
        return _Bundle(
            user_id=user.id,
            organization_id=organization.id,
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            revision_id=revision.id,
        )


def _context(bundle: _Bundle) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="m3f-integration",
                trace_id="m3f-integration",
                user_id=str(bundle.user_id),
            ),
            organization_id=str(bundle.organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(bundle.workspace_id),
        workspace_role=None,
        permissions=frozenset({"knowledge_run"}),
    )


async def _add_revision(
    factory: async_sessionmaker[AsyncSession],
    bundle: _Bundle,
    *,
    status: RevisionIngestionStatus,
    lifecycle: RevisionLifecycleStatus,
    same_document: bool = False,
) -> tuple[UUID, UUID]:
    async with factory() as session:
        document_id = bundle.document_id if same_document else uuid4()
        if not same_document:
            session.add(
                Document(
                    id=document_id,
                    workspace_id=bundle.workspace_id,
                    knowledge_base_id=bundle.knowledge_base_id,
                    name=f"document-{document_id}.txt",
                )
            )
            await session.flush()
        revision = DocumentRevision(
            workspace_id=bundle.workspace_id,
            knowledge_base_id=bundle.knowledge_base_id,
            document_id=document_id,
            revision_number=2 if same_document else 1,
            original_filename="source.txt",
            blob_key=f"m3f/{uuid4().hex}",
            media_type="text/plain",
            file_size=10,
            ingestion_status=status,
            lifecycle_status=lifecycle,
        )
        session.add(revision)
        await session.commit()
        return document_id, revision.id


async def _set_revision_state(
    factory: async_sessionmaker[AsyncSession],
    revision_id: UUID,
    *,
    status: RevisionIngestionStatus,
    lifecycle: RevisionLifecycleStatus,
) -> None:
    async with factory() as session:
        await session.execute(
            update(DocumentRevision)
            .where(DocumentRevision.id == revision_id)
            .values(ingestion_status=status, lifecycle_status=lifecycle)
        )
        await session.commit()


async def _snapshot_items(
    factory: async_sessionmaker[AsyncSession], snapshot_id: UUID
) -> list[KnowledgeSnapshotItem]:
    async with factory() as session:
        return list(
            await session.scalars(
                select(KnowledgeSnapshotItem)
                .where(KnowledgeSnapshotItem.snapshot_id == snapshot_id)
                .order_by(KnowledgeSnapshotItem.document_id)
            )
        )


@pytest.mark.asyncio
async def test_snapshot_api_materializes_and_reuses_current_snapshot(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    settings = Settings(
        testing=True,
        database_url=async_database_url(TEST_DATABASE_URL),
    )
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db
    token = issue_access_token(bundle.user_id, settings)
    path = (
        f"/api/v1/workspaces/{bundle.workspace_id}/"
        f"knowledge-bases/{bundle.knowledge_base_id}/snapshots"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(path, headers={"Authorization": f"Bearer {token}"})
        second = await client.post(path, headers={"Authorization": f"Bearer {token}"})

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["item_count"] == 1
    assert first.json()["snapshot_schema_version"] == 1


@pytest.mark.asyncio
async def test_latest_includes_only_active_ready_revisions_and_empty_kb_is_valid(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    for status, lifecycle in (
        (RevisionIngestionStatus.PENDING, RevisionLifecycleStatus.ACTIVE),
        (RevisionIngestionStatus.PROCESSING, RevisionLifecycleStatus.ACTIVE),
        (RevisionIngestionStatus.FAILED, RevisionLifecycleStatus.ACTIVE),
        (RevisionIngestionStatus.READY, RevisionLifecycleStatus.RETIRED),
        (RevisionIngestionStatus.READY, RevisionLifecycleStatus.DELETED),
    ):
        await _add_revision(
            db_factory,
            bundle,
            status=status,
            lifecycle=lifecycle,
        )
    service = KnowledgeSnapshotService()
    async with db_factory() as session:
        snapshot = await service.create_current_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
        )
    items = await _snapshot_items(db_factory, snapshot.snapshot_id)
    assert [item.document_revision_id for item in items] == [bundle.revision_id]

    empty_bundle = await _seed_bundle(db_factory)
    await _set_revision_state(
        db_factory,
        empty_bundle.revision_id,
        status=RevisionIngestionStatus.PENDING,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
    )
    async with db_factory() as session:
        empty = await service.create_current_snapshot(
            session,
            _context(empty_bundle),
            empty_bundle.knowledge_base_id,
        )
    assert empty.item_count == 0
    assert await _snapshot_items(db_factory, empty.snapshot_id) == []


@pytest.mark.asyncio
async def test_historical_snapshot_resolves_retired_revision_and_latest_moves_forward(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    service = KnowledgeSnapshotService()
    async with db_factory() as session:
        historical = await service.create_current_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
        )
    revision_two_document, revision_two_id = await _add_revision(
        db_factory,
        bundle,
        status=RevisionIngestionStatus.PENDING,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
        same_document=True,
    )
    await _set_revision_state(
        db_factory,
        bundle.revision_id,
        status=RevisionIngestionStatus.READY,
        lifecycle=RevisionLifecycleStatus.RETIRED,
    )
    await _set_revision_state(
        db_factory,
        revision_two_id,
        status=RevisionIngestionStatus.READY,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
    )

    async with db_factory() as session:
        resolved_historical = await service.resolve_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
            historical.snapshot_id,
        )
        latest = await service.resolve_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
            "LATEST",
        )

    historical_items = await _snapshot_items(db_factory, resolved_historical.snapshot_id)
    latest_items = await _snapshot_items(db_factory, latest.snapshot_id)
    assert historical_items[0].document_revision_id == bundle.revision_id
    assert latest_items[0].document_revision_id == revision_two_id
    assert revision_two_document == bundle.document_id
    assert latest.snapshot_id != historical.snapshot_id


@pytest.mark.asyncio
async def test_duplicate_active_ready_revisions_are_rejected(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    await _add_revision(
        db_factory,
        bundle,
        status=RevisionIngestionStatus.READY,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
        same_document=True,
    )
    service = KnowledgeSnapshotService()

    with pytest.raises(AgentHubError) as raised:
        async with db_factory() as session:
            await service.create_current_snapshot(
                session,
                _context(bundle),
                bundle.knowledge_base_id,
            )

    assert raised.value.code == "KNOWLEDGE_REVISION_STATE_CONFLICT"
    assert raised.value.status_code == 409


class _BarrierSnapshotService(KnowledgeSnapshotService):
    def __init__(self, barrier: asyncio.Barrier) -> None:
        self.barrier = barrier

    async def _current_revisions(self, session, workspace_id, knowledge_base_id):
        revisions = await super()._current_revisions(session, workspace_id, knowledge_base_id)
        await self.barrier.wait()
        return revisions


@pytest.mark.asyncio
async def test_concurrent_snapshot_creation_is_idempotent(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    barrier = asyncio.Barrier(2)

    async def create_one() -> UUID:
        async with db_factory() as session:
            result = await _BarrierSnapshotService(barrier).create_current_snapshot(
                session,
                _context(bundle),
                bundle.knowledge_base_id,
            )
            return result.snapshot_id

    first, second = await asyncio.gather(create_one(), create_one())
    assert first == second
    assert len(await _snapshot_items(db_factory, first)) == 1


@pytest.mark.asyncio
async def test_concurrent_ingestion_yields_one_complete_revision_membership(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    _document_two, revision_two_id = await _add_revision(
        db_factory,
        bundle,
        status=RevisionIngestionStatus.PENDING,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
        same_document=True,
    )
    barrier = asyncio.Barrier(2)

    async def create_snapshot() -> UUID:
        async with db_factory() as session:
            result = await _BarrierSnapshotService(barrier).create_current_snapshot(
                session,
                _context(bundle),
                bundle.knowledge_base_id,
            )
            return result.snapshot_id

    async def finalize_revision() -> None:
        await barrier.wait()
        await _set_revision_state(
            db_factory,
            bundle.revision_id,
            status=RevisionIngestionStatus.READY,
            lifecycle=RevisionLifecycleStatus.RETIRED,
        )
        await _set_revision_state(
            db_factory,
            revision_two_id,
            status=RevisionIngestionStatus.READY,
            lifecycle=RevisionLifecycleStatus.ACTIVE,
        )

    snapshot_id, _ = await asyncio.gather(create_snapshot(), finalize_revision())
    items = await _snapshot_items(db_factory, snapshot_id)
    assert len(items) == 1
    assert items[0].document_revision_id in {bundle.revision_id, revision_two_id}


@pytest.mark.asyncio
async def test_cross_workspace_and_cross_kb_resolution_is_scoped(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_bundle(db_factory)
    second = await _seed_bundle(db_factory)
    service = KnowledgeSnapshotService()
    async with db_factory() as session:
        first_snapshot = await service.create_current_snapshot(
            session,
            _context(first),
            first.knowledge_base_id,
        )

    with pytest.raises(AgentHubError) as workspace_error:
        async with db_factory() as session:
            await service.resolve_snapshot(
                session,
                _context(second),
                second.knowledge_base_id,
                first_snapshot.snapshot_id,
            )
    with pytest.raises(AgentHubError) as kb_error:
        async with db_factory() as session:
            await service.resolve_snapshot(
                session,
                _context(first),
                second.knowledge_base_id,
                first_snapshot.snapshot_id,
            )

    assert workspace_error.value.code == "SNAPSHOT_NOT_FOUND"
    assert kb_error.value.code == "SNAPSHOT_NOT_FOUND"


@pytest.mark.asyncio
async def test_composite_foreign_keys_reject_cross_kb_and_document_revision_mismatch(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    service = KnowledgeSnapshotService()
    async with db_factory() as session:
        snapshot = await service.create_current_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
        )
    other_document_id, other_revision_id = await _add_revision(
        db_factory,
        bundle,
        status=RevisionIngestionStatus.READY,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
    )

    async with db_factory() as session:
        session.add(
            KnowledgeSnapshotItem(
                workspace_id=bundle.workspace_id,
                snapshot_id=snapshot.snapshot_id,
                knowledge_base_id=bundle.knowledge_base_id,
                document_id=other_document_id,
                document_revision_id=bundle.revision_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with db_factory() as session:
        session.add(
            KnowledgeSnapshotItem(
                workspace_id=bundle.workspace_id,
                snapshot_id=snapshot.snapshot_id,
                knowledge_base_id=bundle.knowledge_base_id,
                document_id=bundle.document_id,
                document_revision_id=other_revision_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_snapshot_rejects_two_revisions_for_one_document(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    service = KnowledgeSnapshotService()
    async with db_factory() as session:
        snapshot = await service.create_current_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
        )
    _document_id, second_revision_id = await _add_revision(
        db_factory,
        bundle,
        status=RevisionIngestionStatus.PENDING,
        lifecycle=RevisionLifecycleStatus.ACTIVE,
        same_document=True,
    )

    async with db_factory() as session:
        session.add(
            KnowledgeSnapshotItem(
                workspace_id=bundle.workspace_id,
                snapshot_id=snapshot.snapshot_id,
                knowledge_base_id=bundle.knowledge_base_id,
                document_id=bundle.document_id,
                document_revision_id=second_revision_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_snapshot_item_rejects_cross_knowledge_base_reference(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    bundle = await _seed_bundle(db_factory)
    service = KnowledgeSnapshotService()
    async with db_factory() as session:
        snapshot = await service.create_current_snapshot(
            session,
            _context(bundle),
            bundle.knowledge_base_id,
        )

    async with db_factory() as session:
        other_knowledge_base = KnowledgeBase(
            workspace_id=bundle.workspace_id,
            name=f"other-kb-{uuid4()}",
        )
        session.add(other_knowledge_base)
        await session.flush()
        other_document = Document(
            workspace_id=bundle.workspace_id,
            knowledge_base_id=other_knowledge_base.id,
            name="other.txt",
        )
        session.add(other_document)
        await session.flush()
        other_revision = DocumentRevision(
            workspace_id=bundle.workspace_id,
            knowledge_base_id=other_knowledge_base.id,
            document_id=other_document.id,
            revision_number=1,
            original_filename="other.txt",
            blob_key=f"m3f/{uuid4().hex}",
            media_type="text/plain",
            file_size=10,
            ingestion_status=RevisionIngestionStatus.READY,
            lifecycle_status=RevisionLifecycleStatus.ACTIVE,
        )
        session.add(other_revision)
        await session.flush()
        session.add(
            KnowledgeSnapshotItem(
                workspace_id=bundle.workspace_id,
                snapshot_id=snapshot.snapshot_id,
                knowledge_base_id=other_knowledge_base.id,
                document_id=other_document.id,
                document_revision_id=other_revision.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
