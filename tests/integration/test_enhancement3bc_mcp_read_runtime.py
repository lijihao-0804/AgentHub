"""Enhancement 3C: running an imported READ tool against a real MCP server.

These tests drive the published path — an AgentVersion that froze an MCP
revision, resolved through the same resolver and the same policy a builtin tool
goes through — and count how often the remote was actually consulted. The count
is the interesting assertion in most of them: refusing a call is only
meaningful if the refusal happens before anyone is asked to do anything.

A READ is allowed to fail and is never allowed to be uncertain. Re-reading
costs nothing, so an unanswered read is reported as a plain tool error and the
agent carries on.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import mcp_types
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from mcp import Client
from mcp.server.lowlevel.server import Server
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.mcp.client import McpClientAdapter
from packages.mcp.models import McpAuthType, McpConnection
from packages.mcp.runtime import McpToolExecutor
from packages.mcp.security import McpSecretCipher
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.contracts import ToolResultStatus
from packages.tools.runtime import ToolRuntime
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run Enhancement 3C runtime tests.",
    ),
]

ENDPOINT = "https://remote.example.test/mcp"
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"customer_ref": {"type": "string"}},
    "required": ["customer_ref"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
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


def settings() -> Settings:
    return Settings(testing=True, environment="test", credential_master_key="integration-key")


def mcp_spec(connection_id, *, output_schema: Any = None, **overrides: Any) -> dict[str, Any]:
    spec = {
        "kind": "mcp",
        "identity": "crm_lookup_customer",
        "description": "Look up one customer in the CRM.",
        "input_schema": INPUT_SCHEMA,
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
        "mcp": {
            "connection_id": str(connection_id),
            "tool_name": "lookup_customer",
            "output_schema": output_schema,
        },
    }
    spec.update(overrides)
    return spec


class Remote:
    """An in-process MCP server that counts how often its tool was run."""

    def __init__(
        self,
        *,
        text: str = "Customer One",
        structured: Any = None,
        is_error: bool = False,
        delay: float = 0.0,
        content: list[Any] | None = None,
    ) -> None:
        self.text = text
        self.structured = structured
        self.is_error = is_error
        self.delay = delay
        self.content = content
        self.call_count = 0
        self.seen_arguments: list[dict[str, Any]] = []

    async def _on_call_tool(self, _ctx, params) -> mcp_types.CallToolResult:
        self.call_count += 1
        self.seen_arguments.append(dict(params.arguments or {}))
        if self.delay:
            await asyncio.sleep(self.delay)
        blocks = (
            self.content if self.content is not None else [mcp_types.TextContent(text=self.text)]
        )
        return mcp_types.CallToolResult(
            content=blocks, structuredContent=self.structured, isError=self.is_error
        )

    async def _on_list_tools(self, _ctx, _params) -> mcp_types.ListToolsResult:
        # Deliberately declares no output schema of its own. Whether a result
        # is acceptable is decided by the schema frozen at import, not by
        # whatever the server happens to advertise at call time.
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name="lookup_customer", inputSchema=INPUT_SCHEMA)],
            nextCursor=None,
        )

    def executor(self, factory: async_sessionmaker[AsyncSession]) -> McpToolExecutor:
        server = Server(
            "fake-crm",
            version="1.0.0",
            on_call_tool=self._on_call_tool,
            on_list_tools=self._on_list_tools,
        )

        @asynccontextmanager
        async def client_factory(_target, _secret, _observation):
            async with Client(server, cache=None) as client:
                yield client

        config = settings()
        return McpToolExecutor(
            factory,
            settings=config,
            adapter=McpClientAdapter(settings=config, client_factory=client_factory),
        )


async def published(
    factory: async_sessionmaker[AsyncSession],
    *,
    enabled: bool = True,
    auth_type: McpAuthType = McpAuthType.NONE,
    secret_ciphertext: str | None = None,
    elsewhere: bool = False,
    **spec_overrides: Any,
) -> dict[str, Any]:
    """Seed a workspace whose published agent version binds one remote READ.

    The connection id is chosen up front so the frozen revision can name it
    before the row exists, which is also how a revision can end up naming a
    connection that this workspace cannot reach.
    """

    connection_id = uuid4()
    async with factory() as session:
        base = await _seed(
            session, label=uuid4().hex, tool_spec=mcp_spec(connection_id, **spec_overrides)
        )
        owner = base["workspace_id"]
        if elsewhere:
            neighbour = await _seed(session, label=uuid4().hex)
            owner = neighbour["workspace_id"]
        session.add(
            McpConnection(
                id=connection_id,
                workspace_id=owner,
                name=f"crm-{uuid4().hex[:8]}",
                endpoint_url=ENDPOINT,
                auth_type=auth_type,
                enabled=enabled,
                secret_ciphertext=secret_ciphertext,
                secret_version=1 if secret_ciphertext else None,
                created_by=base["user"].id,
            )
        )
        await session.commit()
    base["connection_id"] = connection_id
    base["context"] = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    return base


async def run_tool(
    factory: async_sessionmaker[AsyncSession],
    base: dict[str, Any],
    remote: Remote,
    arguments: dict[str, Any] | None = None,
):
    runtime = ToolRuntime(
        session_factory=factory, mcp_handler=remote.executor(factory).execute_read
    )
    return await runtime.execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="crm_lookup_customer",
        arguments=arguments if arguments is not None else {"customer_ref": "cust-1"},
        tool_call_id=f"call-{uuid4().hex}",
    )


class ScriptedGateway:
    """A model that asks for the remote tool once, then answers."""

    def __init__(self) -> None:
        self.responses = [
            ModelResponse(
                content="",
                provider="fake",
                model="frozen-model-a",
                tool_calls=(
                    ModelToolCall(
                        name="crm_lookup_customer",
                        arguments={"customer_ref": "cust-1"},
                        provider_tool_call_id="call-1",
                    ),
                ),
            ),
            ModelResponse(content="Found them.", provider="fake", model="frozen-model-a"),
        ]
        self.calls = 0

    async def generate_resolved(self, context, plan, request):
        del context, plan, request
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_published_remote_read_runs_once_and_feeds_the_graph(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote(text="Customer One")
    gateway = ScriptedGateway()
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(
            session_factory=db_factory, mcp_handler=remote.executor(db_factory).execute_read
        ),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )

    result = await service.run(
        base["context"], agent_version_id=base["version"].id, input_text="Who is cust-1?"
    )

    assert (result.status, result.failure_code) == ("SUCCEEDED", None)
    assert remote.call_count == 1
    # The remote is told what the frozen schema allows and nothing else; the
    # workspace this call belongs to is AgentHub's business, not the server's.
    assert remote.seen_arguments == [{"customer_ref": "cust-1"}]


@pytest.mark.asyncio
async def test_remote_text_result_is_plain_untrusted_json(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote(text="Customer One")

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == {"content": ["Customer One"], "structured_content": None}
    # Nothing a remote server said is ever promoted to trusted input.
    assert result.data_trust == "UNTRUSTED"


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_the_remote(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote()

    result = await run_tool(db_factory, base, remote, arguments={"customer_ref": 7})

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert remote.call_count == 0


@pytest.mark.asyncio
async def test_injected_tenant_keys_never_reach_the_remote(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote()

    result = await run_tool(
        db_factory,
        base,
        remote,
        arguments={"customer_ref": "cust-1", "workspace_id": str(base["workspace_id"])},
    )

    assert result.status is ToolResultStatus.ERROR
    assert remote.call_count == 0


@pytest.mark.asyncio
async def test_disabled_connection_stops_the_call_before_it_happens(db_factory) -> None:
    base = await published(db_factory, enabled=False)
    remote = Remote()

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "MCP_CONNECTION_DISABLED"
    assert remote.call_count == 0


@pytest.mark.asyncio
async def test_connection_in_another_workspace_is_simply_not_found(db_factory) -> None:
    # The revision names a connection id that exists, but not here. Scoping the
    # lookup by workspace means the answer is the same as for a fabricated id.
    base = await published(db_factory, elsewhere=True)
    remote = Remote()

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "MCP_CONNECTION_NOT_FOUND"
    assert remote.call_count == 0


@pytest.mark.asyncio
async def test_undecryptable_credential_stops_the_call(db_factory) -> None:
    base = await published(
        db_factory, auth_type=McpAuthType.BEARER, secret_ciphertext="v1:not-a-real-token"
    )
    remote = Remote()

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "MCP_SECRET_DECRYPTION_FAILED"
    assert remote.call_count == 0


@pytest.mark.asyncio
async def test_a_usable_bearer_credential_is_decrypted_at_call_time(db_factory) -> None:
    ciphertext = McpSecretCipher.from_settings(settings()).encrypt("rotated-token")
    base = await published(db_factory, auth_type=McpAuthType.BEARER, secret_ciphertext=ciphertext)
    remote = Remote()

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.SUCCESS
    assert remote.call_count == 1


@pytest.mark.asyncio
async def test_remote_reported_failure_is_a_tool_error(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote(text="no such customer", is_error=True)

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "MCP_TOOL_CALL_FAILED"
    assert remote.call_count == 1


@pytest.mark.asyncio
async def test_a_slow_remote_read_fails_rather_than_hanging(db_factory) -> None:
    base = await published(db_factory, timeout_seconds=1)
    remote = Remote(delay=5)

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    # Whichever bound trips first, a read that did not come back is an error.
    # It is never reported as an uncertain outcome: that concept belongs to
    # writes, where the doubt is about a side effect rather than an answer.
    assert result.error_code in {
        "TOOL_TIMEOUT",
        "MCP_CONNECT_TIMEOUT",
        "MCP_SERVER_UNAVAILABLE",
        "MCP_PROTOCOL_ERROR",
    }


@pytest.mark.asyncio
async def test_an_oversized_result_is_refused_rather_than_truncated(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote(text="x" * 2_000_000)

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "MCP_TOOL_RESULT_TOO_LARGE"


@pytest.mark.asyncio
async def test_a_payload_shape_this_version_cannot_carry_is_refused(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote(content=[mcp_types.ImageContent(data="aGk=", mimeType="image/png")])

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "MCP_TOOL_RESULT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_structured_output_must_match_the_schema_frozen_at_import(db_factory) -> None:
    base = await published(db_factory, output_schema=OUTPUT_SCHEMA)
    remote = Remote(structured={"unexpected": True})
    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.ERROR
    # The server's current idea of its own output does not get to overrule the
    # contract the workspace published against.
    assert result.error_code == "MCP_TOOL_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_conforming_structured_output_is_passed_through(db_factory) -> None:
    base = await published(db_factory, output_schema=OUTPUT_SCHEMA)
    remote = Remote(structured={"name": "Customer One"})

    result = await run_tool(db_factory, base, remote)

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data["structured_content"] == {"name": "Customer One"}


@pytest.mark.asyncio
async def test_builtin_read_still_runs_without_any_remote_wiring(db_factory) -> None:
    builtin_spec = {
        "kind": "builtin",
        "identity": "calculator",
        "description": "Safe arithmetic",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=builtin_spec)
    context = base["context"].model_copy(update={"permissions": frozenset({"tool_run"})})

    # No MCP handler at all: a builtin tool must not have grown a dependency on
    # remote composition just because remote tools now exist.
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=context,
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments={"expression": "2 + 3"},
        tool_call_id=f"call-{uuid4().hex}",
    )

    assert result.status is ToolResultStatus.SUCCESS
