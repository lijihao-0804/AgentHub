"""Enhancement 3C: an imported WRITE tool goes through approval, like any other.

A remote WRITE is not a new kind of thing. It waits for the same approval, is
claimed through the same row, and is executed by the same ActionRuntime that
runs ``create_ticket``. What differs is only what an unanswered call means: a
local action that was cancelled did not happen, while a remote one may have.

The remote here counts how often it was actually asked to act. That count is
the assertion that matters in every test below — especially the one where the
answer is lost, where it must stay at one no matter how many times the run is
resumed.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx2
import mcp_types
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from mcp import Client
from mcp.server.lowlevel.server import Server
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
    ApprovalService,
)
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.mcp.client import McpClientAdapter
from packages.mcp.models import McpAuthType, McpConnection
from packages.mcp.runtime import McpActionExecutor, McpToolExecutor
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run Enhancement 3C approval tests.",
    ),
]

ENDPOINT = "https://remote.example.test/mcp"
TOOL_IDENTITY = "crm_refund_customer"
REMOTE_TOOL = "refund_customer"
ARGUMENTS = {"customer_ref": "cust-1", "amount": "12.50"}
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"customer_ref": {"type": "string"}, "amount": {"type": "string"}},
    "required": ["customer_ref", "amount"],
    "additionalProperties": False,
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


def write_spec(connection_id: Any) -> dict[str, Any]:
    return {
        "kind": "mcp",
        "identity": TOOL_IDENTITY,
        "description": "Refund a customer in the CRM.",
        "input_schema": INPUT_SCHEMA,
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
        "timeout_seconds": 30,
        "mcp": {
            "connection_id": str(connection_id),
            "tool_name": REMOTE_TOOL,
            "output_schema": None,
        },
    }


class Remote:
    """An in-process MCP server that records every act it was asked to perform."""

    def __init__(self, *, lose_answer: bool = False) -> None:
        self.lose_answer = lose_answer
        self.call_count = 0
        self.seen_arguments: list[dict[str, Any]] = []

    async def _on_call_tool(self, _ctx, params) -> mcp_types.CallToolResult:
        self.call_count += 1
        self.seen_arguments.append(dict(params.arguments or {}))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text="refund-9001")],
            structuredContent=None,
            isError=False,
        )

    async def _on_list_tools(self, _ctx, _params) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name=REMOTE_TOOL, inputSchema=INPUT_SCHEMA)], nextCursor=None
        )

    def action_executor(self, factory: async_sessionmaker[AsyncSession]) -> McpActionExecutor:
        server = Server(
            "fake-crm",
            version="1.0.0",
            on_call_tool=self._on_call_tool,
            on_list_tools=self._on_list_tools,
        )
        lose_answer = self.lose_answer

        @asynccontextmanager
        async def client_factory(_target, _secret, observation):
            async with Client(server, cache=None) as client:
                yield _LostAnswer(client, observation) if lose_answer else client

        config = settings()
        return McpActionExecutor(
            McpToolExecutor(
                factory,
                settings=config,
                adapter=McpClientAdapter(settings=config, client_factory=client_factory),
            )
        )


class _LostAnswer:
    """A client whose request reaches the server but whose reply never returns.

    This is the case the whole design exists for: the remote genuinely acted,
    and the connection died before we learned that it had. Nothing local can
    distinguish it from a request that never arrived, which is exactly why the
    answer must be "we do not know" rather than "it failed".
    """

    def __init__(self, inner: Any, observation: Any) -> None:
        self._inner = inner
        self._observation = observation

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._observation.mark_dispatched()
        await self._inner.call_tool(name, arguments)
        raise httpx2.ReadError("the connection was reset before the response was read")


class ScriptedGateway:
    """A model that asks for the remote refund once, then answers."""

    def __init__(self) -> None:
        self.responses = [
            ModelResponse(
                content="",
                provider="fake",
                model="frozen-model-a",
                tool_calls=(
                    ModelToolCall(
                        name=TOOL_IDENTITY,
                        arguments=dict(ARGUMENTS),
                        provider_tool_call_id="call-1",
                    ),
                ),
            ),
            ModelResponse(content="Refunded.", provider="fake", model="frozen-model-a"),
        ]
        self.calls = 0

    async def generate_resolved(self, context, plan, request):
        del context, plan, request
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


async def published(
    factory: async_sessionmaker[AsyncSession], *, enabled: bool = True
) -> dict[str, Any]:
    """Seed a workspace whose published agent version binds one remote WRITE."""

    connection_id = uuid4()
    async with factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=write_spec(connection_id))
        session.add(
            McpConnection(
                id=connection_id,
                workspace_id=base["workspace_id"],
                name=f"crm-{uuid4().hex[:8]}",
                endpoint_url=ENDPOINT,
                auth_type=McpAuthType.NONE,
                enabled=enabled,
                created_by=base["user"].id,
            )
        )
        await session.commit()
    base["connection_id"] = connection_id
    base["context"] = base["context"].model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "tool_run", "workspace_read", "approve_action"}
            )
        }
    )
    return base


def service(
    factory: async_sessionmaker[AsyncSession], gateway: ScriptedGateway, remote: Remote, adapter
) -> AgentRunService:
    return AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(factory),
        action_runtime=ActionRuntime(
            session_factory=factory, mcp_executor=remote.action_executor(factory)
        ),
        checkpoint_adapter=adapter,
    )


@pytest.mark.asyncio
async def test_remote_write_waits_for_approval_then_acts_exactly_once(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote()
    gateway = ScriptedGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    context = base["context"]

    first = await service(db_factory, gateway, remote, adapter).run(
        context, agent_version_id=base["version"].id, input_text="Refund them"
    )
    assert first.status == "WAITING_APPROVAL"
    # Nothing may have reached the remote before a human said yes.
    assert remote.call_count == 0

    approval = (await ApprovalService(db_factory).list(context))[0]
    assert approval.decision_status == ApprovalDecisionStatus.PENDING
    assert approval.tool_identity == TOOL_IDENTITY
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )

    resumed = await service(db_factory, gateway, remote, adapter).resume(
        context, run_id=first.run_id, approval_id=approval.id
    )
    assert resumed.run_id == first.run_id
    assert resumed.status == "SUCCEEDED"
    assert remote.call_count == 1
    # The frozen input schema is a closed object: nothing was added to it on
    # the way out, least of all AgentHub's own identifiers or its idempotency
    # key, which would both break the contract and imply a promise the remote
    # has not made.
    assert remote.seen_arguments == [dict(ARGUMENTS)]

    async with db_factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == first.run_id))
        persisted = await session.get(type(approval), approval.id)
    assert run is not None and run.status == "SUCCEEDED"
    assert persisted is not None
    assert persisted.execution_status == ApprovalExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_denied_remote_write_never_reaches_the_remote(db_factory) -> None:
    base = await published(db_factory)
    remote = Remote()
    gateway = ScriptedGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    context = base["context"]

    first = await service(db_factory, gateway, remote, adapter).run(
        context, agent_version_id=base["version"].id, input_text="Refund them"
    )
    assert first.status == "WAITING_APPROVAL"

    approval = (await ApprovalService(db_factory).list(context))[0]
    decided = await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.DENIED
    )
    assert decided.decision_status == ApprovalDecisionStatus.DENIED

    await service(db_factory, gateway, remote, adapter).resume(
        context, run_id=first.run_id, approval_id=approval.id
    )
    assert remote.call_count == 0

    async with db_factory() as session:
        persisted = await session.get(type(approval), approval.id)
    assert persisted is not None
    assert persisted.execution_status != ApprovalExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_lost_answer_is_unknown_and_resuming_never_calls_twice(db_factory) -> None:
    """The blocker case: the remote acted, we never heard, and we must not re-ask."""

    base = await published(db_factory)
    remote = Remote(lose_answer=True)
    gateway = ScriptedGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    context = base["context"]

    first = await service(db_factory, gateway, remote, adapter).run(
        context, agent_version_id=base["version"].id, input_text="Refund them"
    )
    assert first.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(context))[0]
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )

    resumed = await service(db_factory, gateway, remote, adapter).resume(
        context, run_id=first.run_id, approval_id=approval.id
    )
    assert resumed.status == "NEEDS_ATTENTION"
    assert resumed.failure_code == "ACTION_RECONCILIATION_REQUIRED"
    assert remote.call_count == 1

    # Resuming again must read the recorded doubt, not re-run the refund. A
    # second call here would be a second refund with one approval behind it.
    again = await service(db_factory, gateway, remote, adapter).resume(
        context, run_id=first.run_id, approval_id=approval.id
    )
    assert again.status == "NEEDS_ATTENTION"
    assert remote.call_count == 1

    async with db_factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == first.run_id))
        persisted = await session.get(type(approval), approval.id)
    assert run is not None and run.status == "NEEDS_ATTENTION"
    assert run.failure_code == "ACTION_RECONCILIATION_REQUIRED"
    assert persisted is not None
    assert persisted.execution_status == ApprovalExecutionStatus.UNKNOWN_OUTCOME


@pytest.mark.asyncio
async def test_disabled_connection_fails_definitely_without_dispatching(db_factory) -> None:
    """Refused before the transport: a definite no, not a maybe."""

    base = await published(db_factory, enabled=False)
    remote = Remote()
    gateway = ScriptedGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    context = base["context"]

    first = await service(db_factory, gateway, remote, adapter).run(
        context, agent_version_id=base["version"].id, input_text="Refund them"
    )
    assert first.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(context))[0]
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )

    resumed = await service(db_factory, gateway, remote, adapter).resume(
        context, run_id=first.run_id, approval_id=approval.id
    )
    assert resumed.status != "NEEDS_ATTENTION"
    assert remote.call_count == 0

    async with db_factory() as session:
        persisted = await session.get(type(approval), approval.id)
    assert persisted is not None
    assert persisted.execution_status == ApprovalExecutionStatus.FAILED
    assert persisted.failure_code == "MCP_CONNECTION_DISABLED"


@pytest.mark.asyncio
async def test_builtin_write_is_unchanged_when_a_remote_executor_is_present(db_factory) -> None:
    """Adding remote dispatch must not disturb the action that was already here."""

    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
        "description": "Create an internal support ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "subject": {"type": "string"},
                "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            },
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
        "timeout_seconds": 30,
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(
            Customer(
                workspace_id=base["workspace_id"], customer_ref="cust-1", name="Customer One"
            )
        )
        await session.commit()
    context = base["context"].model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "tool_run", "workspace_read", "approve_action"}
            )
        }
    )

    class TicketGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__()
            self.responses[0] = ModelResponse(
                content="",
                provider="fake",
                model="frozen-model-a",
                tool_calls=(
                    ModelToolCall(
                        name="create_ticket",
                        arguments={"customer_ref": "cust-1", "subject": "Payment issue"},
                        provider_tool_call_id="call-1",
                    ),
                ),
            )

    remote = Remote()
    gateway = TicketGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)

    first = await service(db_factory, gateway, remote, adapter).run(
        context, agent_version_id=base["version"].id, input_text="Open ticket"
    )
    assert first.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(context))[0]
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    resumed = await service(db_factory, gateway, remote, adapter).resume(
        context, run_id=first.run_id, approval_id=approval.id
    )
    assert resumed.status == "SUCCEEDED"
    # The remote executor was registered and simply never consulted, because
    # dispatch follows where the tool's body lives, not what is available.
    assert remote.call_count == 0

    async with db_factory() as session:
        tickets = list(
            await session.scalars(
                select(Ticket).where(Ticket.workspace_id == base["workspace_id"])
            )
        )
    assert len(tickets) == 1
