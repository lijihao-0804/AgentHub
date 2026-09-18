from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.control_plane.models import Organization, User, Workspace
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M2 PostgreSQL integration tests.",
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
        config = Config(str(Path("alembic.ini")))
        command.upgrade(config, "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_factory(
    migrated_database: None,
) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


def context_for(workspace_id: UUID) -> WorkspaceExecutionContext:
    principal = PrincipalContext(request_id="m2-integration", trace_id="m2-integration")
    organization = OrganizationContext(
        principal=principal,
        organization_id=str(uuid4()),
        org_role="OWNER",
    )
    return WorkspaceExecutionContext(
        organization=organization,
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
    )


async def create_workspace(session: AsyncSession) -> Workspace:
    user = User(
        email=f"{uuid4()}@example.test",
        normalized_email=f"{uuid4()}@example.test",
        password_hash="not-used-in-this-test",
    )
    session.add(user)
    await session.flush()
    organization = Organization(name=f"M2 {uuid4()}", created_by=user.id)
    session.add(organization)
    await session.flush()
    workspace = Workspace(organization_id=organization.id, name=f"Workspace {uuid4()}")
    session.add(workspace)
    await session.flush()
    return workspace


@pytest.mark.asyncio
async def test_m2_migration_is_current_head(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))

    script = ScriptDirectory.from_config(Config(str(Path("alembic.ini"))))
    assert version == script.get_current_head()


@pytest.mark.asyncio
async def test_model_gateway_repository_is_workspace_scoped(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        first_workspace = await create_workspace(session)
        second_workspace = await create_workspace(session)
        first_credential = ProviderCredential(
            workspace_id=first_workspace.id,
            provider="deepseek",
            name="primary",
            secret="integration-secret-one",
        )
        second_credential = ProviderCredential(
            workspace_id=second_workspace.id,
            provider="openai-compatible",
            name="secondary",
            secret="integration-secret-two",
        )
        session.add_all([first_credential, second_credential])
        await session.flush()
        first_profile = ModelProfile(
            workspace_id=first_workspace.id,
            provider_credential_id=first_credential.id,
            model="deepseek-chat",
            max_tokens=1024,
            timeout_seconds=30,
            capabilities={"streaming": True, "max_context_tokens": 8192},
        )
        session.add(first_profile)
        await session.commit()

        repository = SqlAlchemyModelGatewayRepository(session)
        assert (
            await repository.get_provider_credential(
                context_for(second_workspace.id), first_credential.id
            )
        ) is None
        assert (
            await repository.get_model_profile(context_for(second_workspace.id), first_profile.id)
        ) is None
        assert await repository.list_model_profiles(context_for(second_workspace.id)) == []


@pytest.mark.asyncio
async def test_database_rejects_cross_workspace_profile_references(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        first_workspace = await create_workspace(session)
        second_workspace = await create_workspace(session)
        first_credential = ProviderCredential(
            workspace_id=first_workspace.id,
            provider="deepseek",
            name="primary",
            secret="integration-secret",
        )
        second_credential = ProviderCredential(
            workspace_id=second_workspace.id,
            provider="deepseek",
            name="secondary",
            secret="integration-secret-two",
        )
        session.add_all([first_credential, second_credential])
        await session.flush()
        first_profile = ModelProfile(
            workspace_id=first_workspace.id,
            provider_credential_id=first_credential.id,
            model="deepseek-chat",
            max_tokens=1024,
            timeout_seconds=30,
        )
        session.add(first_profile)
        await session.flush()
        cross_workspace_profile = ModelProfile(
            workspace_id=second_workspace.id,
            provider_credential_id=second_credential.id,
            model="deepseek-chat",
            max_tokens=1024,
            timeout_seconds=30,
            fallback_profile_id=first_profile.id,
        )
        session.add(cross_workspace_profile)

        with pytest.raises(IntegrityError):
            await session.commit()
