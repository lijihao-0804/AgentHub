"""Enhancement 3B: importing a remote tool, and publishing an agent that binds it.

The import is the moment a remote capability becomes something this workspace
governs. These tests hold it to that: the contract is read from the server at
import time rather than taken from the caller, the governance is the operator's
and never the server's, and an agent cannot be published against a connection
that would refuse to answer today.
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
from mcp_types import ListToolsResult, Tool, ToolAnnotations
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentTool, AgentVersion, ToolRevision
from packages.agent_runtime.models import Tool as AgentHubTool
from packages.agent_runtime.publish import AgentPublishService
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.control_plane.rbac import ORGANIZATION_ADMIN_PERMISSIONS, VIEWER_PERMISSIONS
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings, get_settings
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
from packages.model_gateway.models import ModelProfile, ProviderCredential

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run Enhancement 3B+C integration tests.",
    ),
]

ADMIN_PERMISSIONS = ORGANIZATION_ADMIN_PERMISSIONS
ENDPOINT = "https://mcp.example.com/mcp"
TOKEN = "tok-enhancement-3bc-secret"
REMOTE_SCHEMA = {
    "type": "object",
    "properties": {"customer_id": {"type": "string"}},
    "required": ["customer_id"],
}
REMOTE_OUTPUT_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}


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


def context_for(
    user_id: UUID,
    workspace_id: UUID,
    organization_id: UUID,
    permissions: frozenset[str] = ADMIN_PERMISSIONS,
) -> WorkspaceExecutionContext:
    principal = PrincipalContext(
        request_id="enhancement3bc", trace_id="enhancement3bc", user_id=str(user_id)
    )
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal, organization_id=str(organization_id), org_role="OWNER"
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=permissions,
    )


async def create_base(session: AsyncSession) -> dict:
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
    await session.flush()
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider="deepseek",
        name=f"cred-{uuid4()}",
        secret="secret-3bc",
    )
    session.add(credential)
    await session.flush()
    profile = ModelProfile(
        workspace_id=workspace.id,
        provider_credential_id=credential.id,
        model="deepseek-chat",
        max_tokens=2_000,
        timeout_seconds=30,
        capabilities={"tool_calling": True, "streaming": True, "max_context_tokens": 64_000},
    )
    session.add(profile)
    await session.commit()
    return {
        "user_id": user.id,
        "organization_id": organization.id,
        "workspace_id": workspace.id,
        "profile_id": profile.id,
        "context": context_for(user.id, workspace.id, organization.id),
    }


class Remote:
    """An in-process MCP server that records how often it was consulted."""

    def __init__(self, tools: list[Tool]) -> None:
        self.tools = tools
        self.list_calls = 0

    def service(self) -> McpConnectionService:
        async def on_list_tools(_ctx, _params) -> ListToolsResult:
            self.list_calls += 1
            return ListToolsResult(tools=self.tools, nextCursor=None)

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


def remote_with_customer_lookup(**tool_overrides) -> Remote:
    return Remote(
        [
            Tool(
                name="lookup_customer",
                description="Look up one customer in the CRM.",
                inputSchema=REMOTE_SCHEMA,
                outputSchema=REMOTE_OUTPUT_SCHEMA,
                **tool_overrides,
            )
        ]
    )


async def create_connection(
    session: AsyncSession, context: WorkspaceExecutionContext, *, enabled: bool = True
) -> UUID:
    created = await Remote([]).service().create_connection(
        session,
        context,
        name=f"crm-{uuid4()}",
        endpoint_url=ENDPOINT,
        auth_type="BEARER",
        secret=TOKEN,
        enabled=enabled,
    )
    return created["id"]


IMPORT_ARGS = {
    "remote_tool_name": "lookup_customer",
    "identity": "crm_lookup_customer",
    "name": "CRM Customer Lookup",
    "effect": "READ",
    "risk_level": "LOW",
    "approval_policy": "NEVER",
    "timeout_seconds": 30,
}


@pytest.mark.asyncio
async def test_import_freezes_the_contract_the_server_actually_offers(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        remote = remote_with_customer_lookup()

        tool, revision = await remote.service().import_tool(
            session, base["context"], connection_id, **IMPORT_ARGS
        )

        # The listing is taken during the import, not trusted from whatever the
        # browser was shown earlier.
        assert remote.list_calls == 1
        assert revision.revision_number == 1
        assert revision.spec_hash == canonical_json_hash(revision.spec)
        assert revision.spec["kind"] == "mcp"
        assert revision.spec["identity"] == "crm_lookup_customer"
        assert revision.spec["description"] == "Look up one customer in the CRM."
        assert revision.spec["input_schema"] == REMOTE_SCHEMA
        assert revision.spec["mcp"] == {
            "connection_id": str(connection_id),
            "tool_name": "lookup_customer",
            "output_schema": REMOTE_OUTPUT_SCHEMA,
        }
        assert tool.name == "CRM Customer Lookup"
        assert tool.workspace_id == base["workspace_id"]

        # The credential stays on the connection. A revision is readable by
        # anyone who can read the workspace's tools.
        assert TOKEN not in str(revision.spec)
        for forbidden in ("endpoint_url", "authorization", "secret", "token", "headers"):
            assert forbidden not in str(revision.spec).lower().replace("tool_name", "")


@pytest.mark.asyncio
async def test_the_remote_cannot_talk_its_way_out_of_governance(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        # A server claiming to be harmless and idempotent.
        remote = remote_with_customer_lookup(
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True
            )
        )

        _, revision = await remote.service().import_tool(
            session,
            base["context"],
            connection_id,
            **{
                **IMPORT_ARGS,
                "identity": "crm_write_customer",
                "effect": "WRITE",
                "risk_level": "HIGH",
                "approval_policy": "ALWAYS",
            },
        )

        assert revision.spec["effect"] == "WRITE"
        assert revision.spec["risk_level"] == "HIGH"
        assert revision.spec["approval_policy"] == "ALWAYS"
        assert "annotations" not in revision.spec
        assert "remote_annotations" not in revision.spec


@pytest.mark.asyncio
async def test_a_remote_write_may_not_be_imported_as_unattended(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        remote = remote_with_customer_lookup()

        with pytest.raises(AgentHubError) as excinfo:
            await remote.service().import_tool(
                session,
                base["context"],
                connection_id,
                **{**IMPORT_ARGS, "effect": "WRITE", "approval_policy": "NEVER"},
            )

        assert excinfo.value.code == "MCP_TOOL_GOVERNANCE_INVALID"
        assert excinfo.value.status_code == 422
        assert await tool_count(session, base["workspace_id"]) == 0


async def tool_count(session: AsyncSession, workspace_id: UUID) -> int:
    return await session.scalar(
        select(func.count()).select_from(AgentHubTool).where(
            AgentHubTool.workspace_id == workspace_id
        )
    )


@pytest.mark.asyncio
async def test_a_tool_the_server_no_longer_offers_is_not_invented_locally(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        remote = Remote([Tool(name="something_else", inputSchema=REMOTE_SCHEMA)])

        with pytest.raises(AgentHubError) as excinfo:
            await remote.service().import_tool(
                session, base["context"], connection_id, **IMPORT_ARGS
            )

        assert excinfo.value.code == "MCP_TOOL_NOT_FOUND"
        # Nothing half-created: no Tool without the revision that gives it a
        # contract, and no contract nobody agreed to.
        assert await tool_count(session, base["workspace_id"]) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(ToolRevision).where(
                    ToolRevision.workspace_id == base["workspace_id"]
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_an_identity_no_provider_would_accept_is_refused_not_rewritten(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        remote = remote_with_customer_lookup()

        with pytest.raises(AgentHubError) as excinfo:
            await remote.service().import_tool(
                session, base["context"], connection_id, **{**IMPORT_ARGS, "identity": "crm lookup"}
            )

        assert excinfo.value.code == "MCP_TOOL_IDENTITY_INVALID"
        assert await tool_count(session, base["workspace_id"]) == 0
        # And the remote was never consulted for a request that could not succeed.
        assert remote.list_calls == 0


@pytest.mark.asyncio
async def test_a_connection_in_another_workspace_cannot_be_imported_from(db_factory) -> None:
    async with db_factory() as session:
        owner = await create_base(session)
        connection_id = await create_connection(session, owner["context"])
        outsider = await create_base(session)
        remote = remote_with_customer_lookup()

        with pytest.raises(AgentHubError) as excinfo:
            await remote.service().import_tool(
                session, outsider["context"], connection_id, **IMPORT_ARGS
            )

        assert (excinfo.value.code, excinfo.value.status_code) == (
            "MCP_CONNECTION_NOT_FOUND",
            404,
        )
        assert remote.list_calls == 0


@pytest.mark.asyncio
async def test_a_disabled_connection_is_never_reached_at_import(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"], enabled=False)
        remote = remote_with_customer_lookup()

        with pytest.raises(AgentHubError) as excinfo:
            await remote.service().import_tool(
                session, base["context"], connection_id, **IMPORT_ARGS
            )

        assert (excinfo.value.code, excinfo.value.status_code) == (
            "MCP_CONNECTION_DISABLED",
            409,
        )
        assert remote.list_calls == 0


@pytest.mark.asyncio
async def test_importing_requires_the_permission_that_creates_tools(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        viewer = context_for(
            base["user_id"], base["workspace_id"], base["organization_id"], VIEWER_PERMISSIONS
        )
        remote = remote_with_customer_lookup()

        with pytest.raises(AgentHubError) as excinfo:
            await remote.service().import_tool(session, viewer, connection_id, **IMPORT_ARGS)

        assert excinfo.value.status_code == 403
        assert remote.list_calls == 0


@pytest.mark.asyncio
async def test_an_imported_tool_binds_and_publishes_like_any_other(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        tool, revision = await remote_with_customer_lookup().service().import_tool(
            session, base["context"], connection_id, **IMPORT_ARGS
        )
        publish = AgentPublishService()
        agent = await publish.create_draft(
            session,
            base["context"],
            name="Support Agent",
            system_prompt="Answer from the CRM.",
            model_profile_id=base["profile_id"],
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=revision.id,
            )
        )
        await session.commit()

        published = await publish.publish(session, base["context"], agent.id)

        version = await session.get(AgentVersion, published.id)
        assert version is not None
        frozen = version.resolved_spec["tools"]
        assert len(frozen) == 1
        assert frozen[0]["tool_revision_id"] == str(revision.id)
        assert frozen[0]["tool_spec_hash"] == revision.spec_hash
        assert frozen[0]["source_kind"] == "mcp"
        assert frozen[0]["effect"] == "READ"
        # The version pins the contract by hash. It does not copy the endpoint
        # or the schema, so there is one place the truth lives.
        assert "endpoint_url" not in str(frozen[0])
        assert "input_schema" not in frozen[0]


@pytest.mark.asyncio
async def test_publishing_fails_closed_when_the_connection_was_switched_off(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        tool, revision = await remote_with_customer_lookup().service().import_tool(
            session, base["context"], connection_id, **IMPORT_ARGS
        )
        publish = AgentPublishService()
        agent = await publish.create_draft(
            session,
            base["context"],
            name="Support Agent",
            system_prompt="Answer from the CRM.",
            model_profile_id=base["profile_id"],
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=revision.id,
            )
        )
        await session.commit()
        await Remote([]).service().patch_connection(
            session, base["context"], connection_id, {"enabled": False}
        )

        with pytest.raises(AgentHubError) as excinfo:
            await publish.publish(session, base["context"], agent.id)

        assert (excinfo.value.code, excinfo.value.status_code) == (
            "MCP_CONNECTION_DISABLED",
            409,
        )


@pytest.mark.asyncio
async def test_publishing_fails_closed_when_the_connection_is_gone(db_factory) -> None:
    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        tool, revision = await remote_with_customer_lookup().service().import_tool(
            session, base["context"], connection_id, **IMPORT_ARGS
        )
        publish = AgentPublishService()
        agent = await publish.create_draft(
            session,
            base["context"],
            name="Support Agent",
            system_prompt="Answer from the CRM.",
            model_profile_id=base["profile_id"],
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=revision.id,
            )
        )
        await session.commit()
        connection = await session.get(McpConnection, connection_id)
        await session.delete(connection)
        await session.commit()

        with pytest.raises(AgentHubError) as excinfo:
            await publish.publish(session, base["context"], agent.id)

        assert (excinfo.value.code, excinfo.value.status_code) == (
            "MCP_CONNECTION_NOT_FOUND",
            404,
        )


@pytest.mark.asyncio
async def test_two_tools_answering_to_one_identity_cannot_be_published(db_factory) -> None:
    """Identity is how the model asks and how the runtime dispatches.

    Two bound tools sharing one identity leave the dispatcher to pick by row
    order, which is not a decision a database should be making. Publish is the
    last point at which a person can still be told, so it is told here.
    """

    async with db_factory() as session:
        base = await create_base(session)
        connection_id = await create_connection(session, base["context"])
        service = remote_with_customer_lookup().service()
        first_tool, first_revision = await service.import_tool(
            session, base["context"], connection_id, **{**IMPORT_ARGS, "name": "CRM Lookup A"}
        )
        second_tool, second_revision = await service.import_tool(
            session, base["context"], connection_id, **{**IMPORT_ARGS, "name": "CRM Lookup B"}
        )
        assert first_tool.id != second_tool.id
        publish = AgentPublishService()
        agent = await publish.create_draft(
            session,
            base["context"],
            name="Support Agent",
            system_prompt="Answer from the CRM.",
            model_profile_id=base["profile_id"],
        )
        for tool, revision in ((first_tool, first_revision), (second_tool, second_revision)):
            session.add(
                AgentTool(
                    workspace_id=base["workspace_id"],
                    agent_id=agent.id,
                    tool_id=tool.id,
                    tool_revision_id=revision.id,
                )
            )
        await session.commit()

        with pytest.raises(AgentHubError) as excinfo:
            await publish.publish(session, base["context"], agent.id)

        assert (excinfo.value.code, excinfo.value.status_code) == (
            "DUPLICATE_TOOL_IDENTITY",
            422,
        )
        # Nothing was frozen: a refused publish leaves no version behind.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentVersion)
                .where(AgentVersion.agent_id == agent.id)
            )
            == 0
        )


def test_the_import_request_will_not_accept_a_contract_from_its_caller() -> None:
    from pydantic import ValidationError

    from apps.api.schemas.mcp_connections import McpToolImportRequest

    for smuggled in (
        {"input_schema": {"type": "object"}},
        {"output_schema": {"type": "object"}},
        {"description": "anything"},
        {"connection_id": str(uuid4())},
        {"endpoint_url": ENDPOINT},
        {"remote_annotations": {"readOnlyHint": True}},
        {"spec": {}},
        {"spec_hash": "x"},
        {"revision_number": 1},
    ):
        with pytest.raises(ValidationError):
            McpToolImportRequest(**{**IMPORT_ARGS, **smuggled})

    assert McpToolImportRequest(**IMPORT_ARGS).identity == "crm_lookup_customer"
