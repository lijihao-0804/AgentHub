from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.events import AgentEventType
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M7.5-C PostgreSQL integration tests.",
    ),
]


def _async_database_url(database_url: str) -> str:
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
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class ScriptedGateway:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.calls = 0

    async def generate_resolved(self, context, plan, request):
        del context, plan, request
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def _create_ticket_spec() -> dict[str, object]:
    return {
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


@pytest.mark.asyncio
async def test_normal_stream_emits_monotonic_events_and_completes(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex)

    gateway = ScriptedGateway(
        [ModelResponse(content="hello", provider="fake", model="frozen-model-a")]
    )
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
    )
    events = [
        event
        async for event in service.stream(
            base["context"],
            agent_version_id=base["version"].id,
            input_text="hello",
        )
    ]

    assert events[0].type is AgentEventType.RUN_STARTED
    assert events[0].sequence == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type is AgentEventType.RUN_COMPLETED
    assert events[0].payload == {
        "status": "RUNNING",
        "model_step_count": 0,
        "tool_call_count": 0,
    }
    async with db_factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == events[0].run_id))
    assert run is not None
    assert run.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_approval_stream_lists_same_run_and_resumes_without_new_run(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(
            session,
            label=uuid4().hex,
            tool_spec=_create_ticket_spec(),
        )
        session.add(
            Customer(
                workspace_id=base["workspace_id"],
                customer_ref="cust-1",
                name="Customer One",
            )
        )
        await session.commit()

    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="frozen-model-a",
                tool_calls=(
                    ModelToolCall(
                        "create_ticket",
                        {
                            "customer_ref": "cust-1",
                            "subject": "Payment issue",
                            "priority": "HIGH",
                        },
                    ),
                ),
            ),
            ModelResponse(content="Ticket created.", provider="fake", model="frozen-model-a"),
        ]
    )
    approval_service = ApprovalService(db_factory)
    checkpoint_adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=approval_service,
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=checkpoint_adapter,
    )
    run_context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    events = [
        event
        async for event in service.stream(
            run_context,
            agent_version_id=base["version"].id,
            input_text="Open ticket",
        )
    ]

    assert [event.type for event in events].count(AgentEventType.APPROVAL_REQUIRED) == 1
    assert AgentEventType.RUN_COMPLETED not in {event.type for event in events}
    assert AgentEventType.RUN_FAILED not in {event.type for event in events}
    approval_event = next(
        event for event in events if event.type is AgentEventType.APPROVAL_REQUIRED
    )
    assert set(approval_event.payload) == {
        "approval_id",
        "logical_action_id",
        "tool_identity",
        "risk_level",
        "decision_status",
        "execution_status",
    }

    async with db_factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == approval_event.run_id))
    assert run is not None
    assert run.status == "WAITING_APPROVAL"

    admin_context = run_context.model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "tool_run", "workspace_read", "approve_action"}
            )
        }
    )
    approvals = await approval_service.list_for_run(admin_context, run.id)
    assert len(approvals) == 1
    approval = approvals[0]
    assert str(approval.id) == approval_event.payload["approval_id"]
    assert approval.run_id == run.id
    assert approval.decision_status == "PENDING"

    other_context = (await _other_workspace_context(db_factory))[0]
    with pytest.raises(AgentHubError) as isolated:
        await approval_service.list_for_run(other_context, run.id)
    assert isolated.value.code == "AGENT_RUN_NOT_FOUND"
    with pytest.raises(AgentHubError) as missing:
        await approval_service.list_for_run(admin_context, uuid4())
    assert missing.value.code == "AGENT_RUN_NOT_FOUND"

    await approval_service.decide(
        admin_context,
        approval.id,
        decision=ApprovalDecisionStatus.APPROVED,
    )
    resumed = await service.resume(
        admin_context,
        run_id=run.id,
        approval_id=approval.id,
    )
    assert resumed.run_id == run.id
    assert resumed.status == "SUCCEEDED"
    assert gateway.calls == 2


async def _other_workspace_context(
    db_factory: async_sessionmaker[AsyncSession],
) -> tuple[object, object]:
    async with db_factory() as session:
        other = await _seed(session, label=uuid4().hex)
    return other["context"].model_copy(update={"permissions": frozenset({"workspace_read"})}), other


@pytest.mark.asyncio
async def test_run_scoped_approval_query_requires_workspace_read(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex)

    service = ApprovalService(db_factory)
    with pytest.raises(AgentHubError) as denied:
        await service.list_for_run(base["context"], uuid4())
    assert denied.value.code == "FORBIDDEN"
