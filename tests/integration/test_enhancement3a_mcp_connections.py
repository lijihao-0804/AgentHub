"""Enhancement 3A: MCP connections against a real database.

Two properties need a database to mean anything: that a bearer token is
ciphertext in the row an operator could read, and that discovery leaves the
tool catalog exactly as it found it. Both are asserted here against real SQL,
with the remote itself faked in-process so the suite needs no network.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from mcp import Client
from mcp.server.lowlevel.server import Server
from mcp_types import ListToolsResult, Tool
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import Agent, ToolRevision
from packages.agent_runtime.models import Tool as AgentHubTool
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.control_plane.rbac import (
    DEVELOPER_PERMISSIONS,
    ORGANIZATION_ADMIN_PERMISSIONS,
    VIEWER_PERMISSIONS,
)
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.mcp.client import McpClientAdapter
from packages.mcp.models import McpConnection
from packages.mcp.service import McpConnectionService

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run Enhancement 3A integration tests.",
    ),
]

# What resolve_permissions actually hands an owner or organization admin.
# Using the real grant keeps these tests honest about who can do what in
# production, rather than about a set no principal is ever issued.
ADMIN_PERMISSIONS = ORGANIZATION_ADMIN_PERMISSIONS

ENDPOINT = "https://mcp.example.com/mcp"
TOKEN = "tok-enhancement-3a-secret"
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


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
    user_id: UUID, workspace_id: UUID, organization_id: UUID, permissions: frozenset[str]
) -> WorkspaceExecutionContext:
    principal = PrincipalContext(
        request_id="enhancement3a", trace_id="enhancement3a", user_id=str(user_id)
    )
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal, organization_id=str(organization_id), org_role="OWNER"
        ),
        workspace_id=str(workspace_id),
        permissions=permissions,
    )


async def create_workspace(session: AsyncSession) -> tuple[User, Workspace, UUID]:
    email = f"{uuid4()}@example.test"
    user = User(email=email, normalized_email=email, password_hash="not-used")
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


def service_with_fake_remote(tools: list[Tool] | None = None) -> McpConnectionService:
    """A service whose adapter talks to an in-process MCP server."""

    async def on_list_tools(_ctx, _params) -> ListToolsResult:
        return ListToolsResult(tools=tools or [], nextCursor=None)

    server = Server("fake-remote", version="1.2.3", on_list_tools=on_list_tools)

    @asynccontextmanager
    async def factory(_target, _secret, _observation):
        async with Client(server, cache=None) as client:
            yield client

    settings = Settings(
        testing=True, environment="test", credential_master_key="integration-master-key"
    )
    return McpConnectionService(
        settings=settings,
        adapter=McpClientAdapter(settings=settings, client_factory=factory),
    )


def service() -> McpConnectionService:
    return service_with_fake_remote()


@pytest.mark.asyncio
async def test_a_connection_is_created_listed_and_read_back_without_its_secret(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)

        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="BEARER",
            secret=TOKEN,
            enabled=True,
        )

        assert created["secret_configured"] is True
        assert created["endpoint_url"] == ENDPOINT
        assert TOKEN not in repr(created)
        for forbidden in ("secret", "secret_ciphertext", "authorization", "headers"):
            assert forbidden not in created

        listed = await service().list_connections(session, admin)
        assert [item["id"] for item in listed] == [created["id"]]
        assert TOKEN not in repr(listed)

        fetched = await service().get_connection(session, admin, created["id"])
        assert fetched["id"] == created["id"]


@pytest.mark.asyncio
async def test_the_bearer_token_is_ciphertext_in_the_row(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)

        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="BEARER",
            secret=TOKEN,
            enabled=True,
        )

        stored = await session.scalar(
            select(McpConnection.secret_ciphertext).where(McpConnection.id == created["id"])
        )
        assert stored is not None
        assert stored.startswith("v1:")
        assert TOKEN not in stored

        # Nothing anywhere in the row leaks it either.
        row = await session.execute(
            text("SELECT * FROM mcp_connections WHERE id = :id"), {"id": created["id"]}
        )
        assert TOKEN not in str(row.mappings().one())


@pytest.mark.asyncio
async def test_a_connection_is_invisible_from_another_workspace(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=True,
        )

        _, other_workspace, other_org = await create_workspace(session)
        outsider = context_for(
            user.id, other_workspace.id, other_org, ADMIN_PERMISSIONS
        )

        with pytest.raises(AgentHubError) as excinfo:
            await service().get_connection(session, outsider, created["id"])

        # The same answer a nonexistent id gets, so this cannot be used to ask
        # whether some other workspace's connection exists.
        assert excinfo.value.status_code == 404
        assert excinfo.value.code == "MCP_CONNECTION_NOT_FOUND"
        assert await service().list_connections(session, outsider) == []

        with pytest.raises(AgentHubError) as missing:
            await service().get_connection(session, outsider, uuid4())
        assert (missing.value.code, missing.value.status_code) == (
            excinfo.value.code,
            excinfo.value.status_code,
        )


@pytest.mark.asyncio
async def test_roles_are_separated_across_the_lifecycle(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        developer = context_for(user.id, workspace.id, organization_id, DEVELOPER_PERMISSIONS)
        viewer = context_for(user.id, workspace.id, organization_id, VIEWER_PERMISSIONS)

        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="BEARER",
            secret=TOKEN,
            enabled=True,
        )

        # A viewer can see that it exists.
        assert await service().list_connections(session, viewer) != []
        assert (await service().get_connection(session, viewer, created["id"]))["name"] == "primary"

        # An administrator can rename it and replace its token.
        patched = await service().patch_connection(
            session, admin, created["id"], {"name": "renamed"}
        )
        assert patched["name"] == "renamed"
        rotated = await service().rotate_secret(session, admin, created["id"], "tok-rotated")
        assert rotated["secret_configured"] is True

        # A developer can exercise it but not hold its credential.
        outcome = await service_with_fake_remote().test_connection(
            session, developer, created["id"]
        )
        assert outcome["status"] == "healthy"
        with pytest.raises(AgentHubError) as excinfo:
            await service().rotate_secret(session, developer, created["id"], "tok-nope")
        assert excinfo.value.status_code == 403

        # A viewer can do neither.
        with pytest.raises(AgentHubError) as viewer_test:
            await service().test_connection(session, viewer, created["id"])
        assert viewer_test.value.status_code == 403


@pytest.mark.asyncio
async def test_rotating_a_secret_changes_the_ciphertext_and_bumps_the_version(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="BEARER",
            secret=TOKEN,
            enabled=True,
        )
        before = await session.scalar(
            select(McpConnection.secret_ciphertext).where(McpConnection.id == created["id"])
        )

        await service().rotate_secret(session, admin, created["id"], "tok-rotated")

        after = await session.scalar(
            select(McpConnection.secret_ciphertext).where(McpConnection.id == created["id"])
        )
        version = await session.scalar(
            select(McpConnection.secret_version).where(McpConnection.id == created["id"])
        )
        assert after != before
        assert "tok-rotated" not in after
        assert version == 2


@pytest.mark.asyncio
async def test_a_connection_without_bearer_auth_has_no_secret_to_rotate(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="open",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=True,
        )

        with pytest.raises(AgentHubError) as excinfo:
            await service().rotate_secret(session, admin, created["id"], "tok-new")

        assert excinfo.value.status_code == 409
        assert excinfo.value.code == "MCP_AUTH_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_the_endpoint_cannot_be_patched(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=True,
        )

        for repoint in ({"endpoint_url": "https://evil.example.com/mcp"}, {"auth_type": "BEARER"}):
            with pytest.raises(AgentHubError) as excinfo:
                await service().patch_connection(session, admin, created["id"], repoint)
            assert excinfo.value.status_code == 422

        unchanged = await service().get_connection(session, admin, created["id"])
        assert unchanged["endpoint_url"] == ENDPOINT
        assert unchanged["auth_type"] == "NONE"


@pytest.mark.asyncio
async def test_a_disabled_connection_is_never_reached(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=False,
        )

        # The adapter here would answer happily if it were ever consulted, so a
        # refusal can only have come from the disabled flag.
        reachable = service_with_fake_remote([Tool(name="search", inputSchema=SCHEMA)])
        for operation in (reachable.test_connection, reachable.discover_tools):
            with pytest.raises(AgentHubError) as excinfo:
                await operation(session, admin, created["id"])
            assert excinfo.value.status_code == 409
            assert excinfo.value.code == "MCP_CONNECTION_DISABLED"


@pytest.mark.asyncio
async def test_discovery_returns_a_catalog_and_persists_nothing(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        developer = context_for(user.id, workspace.id, organization_id, DEVELOPER_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="BEARER",
            secret=TOKEN,
            enabled=True,
        )

        tools_before = await session.scalar(select(func.count()).select_from(AgentHubTool))
        revisions_before = await session.scalar(select(func.count()).select_from(ToolRevision))
        agents_before = await session.scalar(select(func.count()).select_from(Agent))
        connections_before = await session.scalar(
            select(func.count()).select_from(McpConnection)
        )

        remote = service_with_fake_remote(
            [
                Tool(name="search", description="search things", inputSchema=SCHEMA),
                Tool(name="fetch", description="fetch things", inputSchema=SCHEMA),
            ]
        )
        catalog = await remote.discover_tools(session, developer, created["id"])

        assert [entry["name"] for entry in catalog["tools"]] == ["search", "fetch"]
        assert catalog["server_name"] == "fake-remote"
        assert TOKEN not in repr(catalog)

        # Discovery is a read of someone else's server. Nothing here is an
        # AgentHub tool yet, and importing one is 3B's job, not a side effect.
        assert await session.scalar(select(func.count()).select_from(AgentHubTool)) == tools_before
        assert (
            await session.scalar(select(func.count()).select_from(ToolRevision))
            == revisions_before
        )
        assert await session.scalar(select(func.count()).select_from(Agent)) == agents_before
        assert (
            await session.scalar(select(func.count()).select_from(McpConnection))
            == connections_before
        )


@pytest.mark.asyncio
async def test_testing_a_connection_persists_nothing_either(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        created = await service().create_connection(
            session,
            admin,
            name="primary",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=True,
        )
        tools_before = await session.scalar(select(func.count()).select_from(AgentHubTool))

        outcome = await service_with_fake_remote().test_connection(session, admin, created["id"])

        assert outcome["status"] == "healthy"
        assert outcome["failure_code"] is None
        assert outcome["server_name"] == "fake-remote"
        assert outcome["connection_id"] == created["id"]
        assert await session.scalar(select(func.count()).select_from(AgentHubTool)) == tools_before


@pytest.mark.asyncio
async def test_a_name_is_unique_within_a_workspace_but_not_across_them(db_factory) -> None:
    async with db_factory() as session:
        user, workspace, organization_id = await create_workspace(session)
        admin = context_for(user.id, workspace.id, organization_id, ADMIN_PERMISSIONS)
        await service().create_connection(
            session,
            admin,
            name="shared-name",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=True,
        )

        with pytest.raises(AgentHubError) as excinfo:
            await service().create_connection(
                session,
                admin,
                name="shared-name",
                endpoint_url="https://other.example.com/mcp",
                auth_type="NONE",
                secret=None,
                enabled=True,
            )
        assert excinfo.value.status_code == 409

    # A fresh session: the rejected write rolled its own back, and the point
    # here is the constraint's scope, not what a reused session does next.
    async with db_factory() as session:
        other_user, other_workspace, other_org = await create_workspace(session)
        elsewhere = context_for(
            other_user.id, other_workspace.id, other_org, ADMIN_PERMISSIONS
        )
        reused = await service().create_connection(
            session,
            elsewhere,
            name="shared-name",
            endpoint_url=ENDPOINT,
            auth_type="NONE",
            secret=None,
            enabled=True,
        )

        # The name is unique per workspace, not globally: two workspaces naming
        # their connection "shared-name" is normal, not a collision.
        assert reused["name"] == "shared-name"
