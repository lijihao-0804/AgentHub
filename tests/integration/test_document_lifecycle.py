"""The endpoint that records which document replaced which.

``document_revisions.lifecycle_status`` already retires a revision of one
document. Nothing could say that ``password-policy-v1`` was replaced by the
separate document ``password-policy-v2``, so retrieval had no way to prefer the
live one. These tests cover the write side of that link and the ways it can be
asked for something invalid.
"""

from __future__ import annotations

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from packages.control_plane.audit import AuditLog
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
from packages.knowledge.models import Document, KnowledgeBase
from packages.knowledge.services import KnowledgeService

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run document lifecycle integration tests.",
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
    other_knowledge_base_id: UUID
    v1_document_id: UUID
    v2_document_id: UUID
    other_base_document_id: UUID


async def _seed(factory: async_sessionmaker[AsyncSession]) -> _Bundle:
    async with factory() as session:
        email = f"{uuid4()}@lifecycle.test"
        user = User(email=email, normalized_email=email, password_hash="not-used")
        session.add(user)
        await session.flush()
        organization = Organization(name=f"lifecycle-{uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id, user_id=user.id, role="OWNER"
            )
        )
        workspace = Workspace(organization_id=organization.id, name=f"workspace-{uuid4()}")
        session.add(workspace)
        await session.flush()
        base = KnowledgeBase(workspace_id=workspace.id, name="Policies")
        other_base = KnowledgeBase(workspace_id=workspace.id, name="Other policies")
        session.add_all([base, other_base])
        await session.flush()
        v1 = Document(
            workspace_id=workspace.id,
            knowledge_base_id=base.id,
            name="password-policy-v1.md",
        )
        v2 = Document(
            workspace_id=workspace.id,
            knowledge_base_id=base.id,
            name="password-policy-v2.md",
        )
        elsewhere = Document(
            workspace_id=workspace.id,
            knowledge_base_id=other_base.id,
            name="password-policy-v3.md",
        )
        session.add_all([v1, v2, elsewhere])
        await session.commit()
        return _Bundle(
            user_id=user.id,
            organization_id=organization.id,
            workspace_id=workspace.id,
            knowledge_base_id=base.id,
            other_knowledge_base_id=other_base.id,
            v1_document_id=v1.id,
            v2_document_id=v2.id,
            other_base_document_id=elsewhere.id,
        )


def _context(bundle: _Bundle, *, permissions: frozenset[str]) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="lifecycle-integration",
                trace_id="lifecycle-integration",
                user_id=str(bundle.user_id),
            ),
            organization_id=str(bundle.organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(bundle.workspace_id),
        workspace_role=None,
        permissions=permissions,
    )


async def _update(factory, bundle: _Bundle, **kwargs):
    async with factory() as session:
        return await KnowledgeService().update_document_lifecycle(
            session,
            context=kwargs.pop(
                "context", _context(bundle, permissions=frozenset({"knowledge_edit"}))
            ),
            knowledge_base_id=kwargs.pop("knowledge_base_id", bundle.knowledge_base_id),
            document_id=kwargs.pop("document_id", bundle.v1_document_id),
            effective_date=kwargs.pop("effective_date", None),
            superseded_by_document_id=kwargs.pop("superseded_by_document_id", None),
            set_effective_date=kwargs.pop("set_effective_date", False),
            set_superseded_by=kwargs.pop("set_superseded_by", True),
        )


@pytest.mark.asyncio
async def test_supersession_is_recorded_and_audited(db_factory) -> None:
    from datetime import date

    bundle = await _seed(db_factory)
    document = await _update(
        db_factory,
        bundle,
        superseded_by_document_id=bundle.v2_document_id,
        effective_date=date(2024, 3, 1),
        set_effective_date=True,
    )
    assert document.superseded_by_document_id == bundle.v2_document_id
    assert document.effective_date == date(2024, 3, 1)

    async with db_factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.workspace_id == bundle.workspace_id,
                AuditLog.action == "document_lifecycle_update",
            )
        )
    assert audit is not None
    assert audit.safe_metadata["superseded_by_document_id"] == str(bundle.v2_document_id)
    assert audit.safe_metadata["effective_date"] == "2024-03-01"


@pytest.mark.asyncio
async def test_clearing_is_distinguishable_from_leaving_alone(db_factory) -> None:
    from datetime import date

    bundle = await _seed(db_factory)
    await _update(
        db_factory,
        bundle,
        superseded_by_document_id=bundle.v2_document_id,
        effective_date=date(2024, 3, 1),
        set_effective_date=True,
    )
    # Touching only the date must leave the supersession in place.
    document = await _update(
        db_factory,
        bundle,
        effective_date=date(2025, 1, 1),
        set_effective_date=True,
        set_superseded_by=False,
    )
    assert document.superseded_by_document_id == bundle.v2_document_id
    assert document.effective_date == date(2025, 1, 1)

    # Explicit null clears it; the date is untouched because it was omitted.
    document = await _update(db_factory, bundle, superseded_by_document_id=None)
    assert document.superseded_by_document_id is None
    assert document.effective_date == date(2025, 1, 1)


@pytest.mark.asyncio
async def test_a_document_cannot_supersede_itself(db_factory) -> None:
    bundle = await _seed(db_factory)
    with pytest.raises(AgentHubError) as excinfo:
        await _update(
            db_factory, bundle, superseded_by_document_id=bundle.v1_document_id
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "INVALID_DOCUMENT_SUPERSESSION"


@pytest.mark.asyncio
async def test_a_successor_in_another_knowledge_base_is_not_found(db_factory) -> None:
    # A snapshot is built from one knowledge base, so a cross-base successor
    # would be invisible at the only moment the field is read.
    bundle = await _seed(db_factory)
    with pytest.raises(AgentHubError) as excinfo:
        await _update(
            db_factory, bundle, superseded_by_document_id=bundle.other_base_document_id
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_document_in_another_workspace_is_not_found(db_factory) -> None:
    bundle = await _seed(db_factory)
    other = await _seed(db_factory)
    with pytest.raises(AgentHubError) as excinfo:
        await _update(
            db_factory,
            bundle,
            document_id=other.v1_document_id,
            superseded_by_document_id=bundle.v2_document_id,
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_the_existing_edit_permission_is_what_gates_this(db_factory) -> None:
    bundle = await _seed(db_factory)
    with pytest.raises(AgentHubError) as excinfo:
        await _update(
            db_factory,
            bundle,
            context=_context(bundle, permissions=frozenset({"knowledge_run"})),
            superseded_by_document_id=bundle.v2_document_id,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_the_patch_route_applies_and_rejects_unknown_fields(db_factory) -> None:
    bundle = await _seed(db_factory)
    settings = Settings(testing=True, database_url=async_database_url(TEST_DATABASE_URL))
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_db
    token = issue_access_token(bundle.user_id, settings)
    path = (
        f"/api/v1/workspaces/{bundle.workspace_id}/"
        f"knowledge-bases/{bundle.knowledge_base_id}/documents/{bundle.v1_document_id}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        applied = await client.patch(
            path,
            headers=headers,
            json={
                "superseded_by_document_id": str(bundle.v2_document_id),
                "effective_date": "2024-03-01",
            },
        )
        rejected = await client.patch(path, headers=headers, json={"retired": True})
        empty = await client.patch(path, headers=headers, json={})

    assert applied.status_code == 200, applied.text
    assert applied.json()["superseded_by_document_id"] == str(bundle.v2_document_id)
    assert applied.json()["effective_date"] == "2024-03-01"
    assert rejected.status_code == 422, rejected.text
    assert empty.status_code == 422, empty.text
