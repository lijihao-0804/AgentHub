from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import Agent
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.models import ProviderCredential

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7.5-A PostgreSQL integration tests.",
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
    command.upgrade(Config(str(Path("alembic.ini"))), "head")


@pytest_asyncio.fixture
async def db_factory(migrated_database: None) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


def owner_context(
    user_id: UUID, workspace_id: UUID, organization_id: UUID
) -> WorkspaceExecutionContext:
    principal = PrincipalContext(
        request_id="m75a-integration",
        trace_id="m75a-integration",
        user_id=str(user_id),
    )
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal,
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        permissions=frozenset(
            {
                "workspace_administration",
                "workspace_read",
                "agent_edit",
                "tool_create",
                "tool_edit",
            }
        ),
    )


async def create_workspace(session: AsyncSession) -> tuple[User, Workspace, UUID]:
    user = User(
        email=f"{uuid4()}@example.test",
        normalized_email=f"{uuid4()}@example.test",
        password_hash="not-used-in-service-test",
    )
    session.add(user)
    await session.flush()
    organization = Organization(name=f"org-{uuid4()}", created_by=user.id)
    session.add(organization)
    await session.flush()
    session.add(
        OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
    )
    workspace = Workspace(organization_id=organization.id, name=f"workspace-{uuid4()}")
    session.add(workspace)
    await session.commit()
    return user, workspace, organization.id


@pytest.mark.asyncio
async def test_product_control_plane_is_workspace_scoped_and_secret_safe(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        context = owner_context(user.id, workspace.id, organization_id)
        service = ProductControlPlaneService()

        credential = await service.create_provider_credential(
            session,
            context,
            provider="deepseek",
            name="primary",
            secret="m75a-secret-sentinel",
            base_url=None,
            enabled=True,
        )
        assert credential["provider"] == "deepseek"
        assert "secret" not in credential
        raw_secret, ciphertext = (
            await session.execute(
                text("SELECT secret, secret_ciphertext FROM provider_credentials WHERE id = :id"),
                {"id": credential["id"]},
            )
        ).one()
        assert raw_secret is None
        assert "m75a-secret-sentinel" not in ciphertext

        profile = await service.create_model_profile(
            session,
            context,
            {
                "provider_credential_id": credential["id"],
                "model": "deepseek-chat",
                "temperature": 0,
                "max_tokens": 512,
                "timeout_seconds": 30,
                "fallback_profile_id": None,
                "capabilities": {"streaming": True, "max_context_tokens": 8192},
                "enabled": True,
            },
        )
        assert profile.workspace_id == workspace.id

        tool = await service.create_tool(session, context, "calculator")
        assert tool["identity"] == "calculator"
        revisions = await service.list_tool_revisions(session, context, tool["id"])
        assert len(revisions) == 1
        assert revisions[0].spec["identity"] == "calculator"

        agent = Agent(
            workspace_id=workspace.id,
            name="managed-agent",
            system_prompt="Use the configured tools.",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.commit()

        bindings = await service.replace_tool_bindings(
            session,
            context,
            agent.id,
            [{"tool_id": tool["id"], "tool_revision_id": None}],
        )
        assert len(bindings) == 1
        assert bindings[0].tool_revision_id is None


@pytest.mark.asyncio
async def test_invalid_binding_replace_preserves_existing_set(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        context = owner_context(user.id, workspace.id, organization_id)
        service = ProductControlPlaneService()
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="deepseek",
            name="primary",
            secret="seed-secret",
        )
        session.add(credential)
        await session.flush()
        from packages.model_gateway.models import ModelProfile

        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="deepseek-chat",
            max_tokens=256,
            timeout_seconds=30,
        )
        session.add(profile)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name="managed-agent",
            system_prompt="Prompt",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.commit()

        from packages.knowledge.models import KnowledgeBase

        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="kb")
        session.add(knowledge_base)
        await session.commit()
        existing = await service.replace_knowledge_bindings(
            session,
            context,
            agent.id,
            [
                {
                    "knowledge_base_id": knowledge_base.id,
                    "binding_mode": "LATEST",
                    "snapshot_id": None,
                }
            ],
        )
        assert len(existing) == 1

        with pytest.raises(AgentHubError) as raised:
            await service.replace_knowledge_bindings(
                session,
                context,
                agent.id,
                [
                    {
                        "knowledge_base_id": knowledge_base.id,
                        "binding_mode": "LATEST",
                        "snapshot_id": uuid4(),
                    }
                ],
            )
        assert "INVALID_KNOWLEDGE_BINDING" in str(raised.value)
        retained = await service.get_knowledge_bindings(session, context, agent.id)
        assert len(retained) == 1
        assert retained[0].snapshot_id is None
