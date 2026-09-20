from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.schemas.agents import AgentPreflightResponse
from packages.agent_runtime.models import Agent, AgentVersion
from packages.agent_runtime.publish import AgentPublishService
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import ModelHealthResult, ModelHealthStatus
from packages.model_gateway.models import ModelProfile

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7.5-B PostgreSQL integration tests.",
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


def context_for(
    user_id: UUID,
    workspace_id: UUID,
    organization_id: UUID,
    *,
    permissions: frozenset[str] | None = None,
) -> WorkspaceExecutionContext:
    principal = PrincipalContext(
        request_id="m75b-integration",
        trace_id="m75b-integration",
        user_id=str(user_id),
    )
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal,
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=permissions
        or frozenset(
            {
                "agent_create",
                "agent_edit",
                "agent_run",
                "workspace_read",
                "workspace_administration",
            }
        ),
    )


async def create_base(session: AsyncSession, label: str) -> dict[str, object]:
    user = User(
        email=f"m75b-{label}-{uuid4()}@example.test",
        normalized_email=f"m75b-{label}-{uuid4()}@example.test",
        password_hash="not-used-in-m75b",
    )
    session.add(user)
    await session.flush()
    organization = Organization(name=f"M7.5-B {label}", created_by=user.id)
    session.add(organization)
    await session.flush()
    session.add(
        OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
    )
    workspace = Workspace(organization_id=organization.id, name=f"M7.5-B {label} workspace")
    session.add(workspace)
    await session.commit()
    context = context_for(user.id, workspace.id, organization.id)
    control = ProductControlPlaneService()
    credential = await control.create_provider_credential(
        session,
        context,
        provider="deepseek",
        name=f"m75b-{label}",
        secret=f"secret-{label}",
        base_url=None,
        enabled=True,
    )
    profile = await control.create_model_profile(
        session,
        context,
        {
            "provider_credential_id": credential["id"],
            "model": "deepseek-chat",
            "temperature": 0,
            "max_tokens": 512,
            "timeout_seconds": 30,
            "fallback_profile_id": None,
            "capabilities": {"max_context_tokens": 64_000},
            "enabled": True,
        },
    )
    agent = await AgentPublishService().create_draft(
        session,
        context,
        name=f"M7.5-B {label} agent",
        system_prompt="Answer from approved context.",
        model_profile_id=profile.id,
    )
    return {
        "user": user,
        "workspace": workspace,
        "organization": organization,
        "context": context,
        "profile": profile,
        "agent": agent,
    }


class FakeHealthGateway:
    def __init__(self, result: ModelHealthResult) -> None:
        self.result = result
        self.calls: list[UUID] = []

    async def health(self, _context, profile_id: UUID) -> ModelHealthResult:
        self.calls.append(profile_id)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        (ModelHealthStatus.HEALTHY, None),
        (ModelHealthStatus.DEGRADED, "MODEL_RATE_LIMITED"),
        (ModelHealthStatus.UNAVAILABLE, "MODEL_PROVIDER_UNAVAILABLE"),
    ],
)
async def test_model_profile_live_test_is_safe_and_workspace_scoped(
    db_factory,
    status: ModelHealthStatus,
    failure_code: str | None,
) -> None:
    async with db_factory() as session:
        base = await create_base(session, f"health-{status.value}")
        profile = base["profile"]
        context = base["context"]
        assert isinstance(profile, ModelProfile)
        assert isinstance(context, WorkspaceExecutionContext)
        gateway = FakeHealthGateway(ModelHealthResult(status, failure_code))

        result = await ProductControlPlaneService().test_model_profile(
            session, context, profile.id, gateway
        )

        assert result["model_profile_id"] == profile.id
        assert result["status"] == status.value
        assert result["failure_code"] == failure_code
        assert result["latency_ms"] >= 0
        assert gateway.calls == [profile.id]
        assert set(result) == {"model_profile_id", "status", "failure_code", "latency_ms"}

        cross_context = context_for(
            context.user_id and UUID(context.user_id),
            uuid4(),
            uuid4(),
            permissions=frozenset({"agent_edit"}),
        )
        with pytest.raises(AgentHubError) as raised:
            await ProductControlPlaneService().test_model_profile(
                session, cross_context, profile.id, gateway
            )
        assert raised.value.code == "MODEL_PROFILE_NOT_FOUND"
        assert gateway.calls == [profile.id]


@pytest.mark.asyncio
async def test_preflight_is_side_effect_free_and_matches_publish_resolution(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session, "preflight")
        context = base["context"]
        agent = base["agent"]
        assert isinstance(context, WorkspaceExecutionContext)
        assert isinstance(agent, Agent)
        service = AgentPublishService()

        before = await session.scalar(
            select(func.count(AgentVersion.id)).where(AgentVersion.agent_id == agent.id)
        )
        preview = await service.preflight(session, context, agent.id)
        after = await session.scalar(
            select(func.count(AgentVersion.id)).where(AgentVersion.agent_id == agent.id)
        )

        response = AgentPreflightResponse.model_validate(preview)
        assert response.status == "READY"
        assert before == after == 0
        assert response.resolved_spec_hash
        assert response.resolved_spec_hash == canonical_json_hash(response.resolved_spec)

        published = await service.publish(session, context, agent.id)
        assert published.resolved_spec_hash == response.resolved_spec_hash
        assert await session.scalar(
            select(func.count(AgentVersion.id)).where(AgentVersion.agent_id == agent.id)
        ) == 1


@pytest.mark.asyncio
async def test_agent_version_detail_is_immutable_and_workspace_scoped(db_factory) -> None:
    async with db_factory() as session:
        base_a = await create_base(session, "version-a")
        base_b = await create_base(session, "version-b")
        context_a = base_a["context"]
        context_b = base_b["context"]
        agent_a = base_a["agent"]
        agent_b = base_b["agent"]
        assert isinstance(context_a, WorkspaceExecutionContext)
        assert isinstance(context_b, WorkspaceExecutionContext)
        assert isinstance(agent_a, Agent)
        assert isinstance(agent_b, Agent)

        service = AgentPublishService()
        published = await service.publish(session, context_a, agent_a.id)
        detail = await service.get_version(session, context_a, agent_a.id, published.id)
        assert detail.id == published.id
        assert detail.agent_id == agent_a.id
        assert detail.workspace_id == UUID(context_a.workspace_id)
        assert detail.resolved_spec_hash == published.resolved_spec_hash

        viewer_context = context_for(
            UUID(context_a.user_id),
            UUID(context_a.workspace_id),
            UUID(context_a.organization.organization_id),
            permissions=frozenset({"workspace_read"}),
        )
        viewer_detail = await service.get_version(
            session, viewer_context, agent_a.id, published.id
        )
        assert viewer_detail.id == published.id

        with pytest.raises(AgentHubError) as wrong_agent:
            await service.get_version(session, context_a, agent_b.id, published.id)
        assert wrong_agent.value.code == "AGENT_VERSION_NOT_FOUND"

        with pytest.raises(AgentHubError) as wrong_workspace:
            await service.get_version(session, context_b, agent_a.id, published.id)
        assert wrong_workspace.value.code == "AGENT_VERSION_NOT_FOUND"

        with pytest.raises(AgentHubError) as unknown_version:
            await service.get_version(session, context_a, agent_a.id, uuid4())
        assert unknown_version.value.code == "AGENT_VERSION_NOT_FOUND"
