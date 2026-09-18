from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalExecutionStatus, ApprovalService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionRuntime
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
)
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import _seed

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M5 Approval integration tests.",
    ),
]


def _async_database_url(database_url: str) -> str:
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
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class ScriptedApprovalGateway:
    def __init__(self) -> None:
        self.responses = [
            ModelResponse(
                content="",
                provider="fake",
                model="fake-approval",
                tool_calls=(
                    ModelToolCall(
                        name="create_ticket",
                        arguments={
                            "customer_ref": "cust-1",
                            "subject": "Payment issue",
                            "priority": "HIGH",
                        },
                        provider_tool_call_id="provider-call-changes-on-replay",
                    ),
                ),
            ),
            ModelResponse(content="Ticket created.", provider="fake", model="fake-approval"),
        ]
        self.calls = 0

    async def generate_resolved(self, context, plan, request):
        del context, plan, request
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


@pytest.mark.asyncio
async def test_approval_waits_then_resumes_same_run_and_creates_one_ticket(db_factory) -> None:
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
                workspace_id=base["workspace_id"],
                customer_ref="cust-1",
                name="Customer One",
            )
        )
        await session.commit()

    gateway = ScriptedApprovalGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=adapter,
    )
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    first = await service.run(
        context, agent_version_id=base["version"].id, input_text="Open ticket"
    )
    assert first.status == "WAITING_APPROVAL"

    admin_context = context.model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "tool_run", "workspace_read", "approve_action"}
            )
        }
    )
    approvals = await ApprovalService(db_factory).list(admin_context)
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.decision_status == ApprovalDecisionStatus.PENDING
    assert approval.canonical_arguments["subject"] == "Payment issue"

    decided = await ApprovalService(db_factory).decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    assert decided.decision_status == ApprovalDecisionStatus.APPROVED

    restarted = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=adapter,
    )
    resumed = await restarted.resume(admin_context, run_id=first.run_id, approval_id=approval.id)
    assert resumed.run_id == first.run_id
    assert resumed.status == "SUCCEEDED"

    async with db_factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == first.run_id))
        tickets = list(
            await session.scalars(
                select(Ticket).where(Ticket.workspace_id == base["workspace_id"])
            )
        )
        refreshed = await session.get(type(approval), approval.id)
    assert run is not None and run.status == "SUCCEEDED"
    assert len(tickets) == 1
    assert refreshed is not None
    assert refreshed.execution_status == ApprovalExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_winner_and_ticket_is_effectively_once(db_factory) -> None:
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
        "input_schema": {
            "type": "object",
            "properties": {"customer_ref": {"type": "string"}, "subject": {"type": "string"}},
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(
            Customer(
                workspace_id=base["workspace_id"], customer_ref="cust-claim", name="Claim Customer"
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
    async with db_factory() as session:
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            input_text="claim",
            created_by=base["user"].id,
        )
        session.add(run)
        await session.commit()
    approvals = ApprovalService(db_factory)
    approval = await approvals.create_or_get(
        context,
        run_id=run.id,
        agent_version_id=base["version"].id,
        tool_revision_id=base["revision"].id,
        tool_identity="create_ticket",
        arguments={"customer_ref": "cust-claim", "subject": "Claim me"},
        input_schema=tool_spec["input_schema"],
        proposal_ordinal=0,
    )
    await approvals.decide(context, approval.id, decision=ApprovalDecisionStatus.APPROVED)
    claimed = await asyncio.gather(
        approvals.claim_execution(context, approval.id),
        approvals.claim_execution(context, approval.id),
    )
    assert sum(item is not None for item in claimed) == 1

    definition = ToolDefinition(
        identity="create_ticket",
        revision_id=base["revision"].id,
        spec_hash=base["revision"].spec_hash,
        description="Create a ticket",
        input_schema=tool_spec["input_schema"],
        effect=ToolEffect.WRITE,
        risk_level=ToolRisk.HIGH,
        approval_policy=ToolApprovalPolicy.ALWAYS,
        timeout_seconds=30,
    )
    action_runtime = ActionRuntime(session_factory=db_factory)
    results = await asyncio.gather(
        action_runtime.execute(
            context,
            definition,
            {"customer_ref": "cust-claim", "subject": "Idempotent"},
            idempotency_key="logical-action-ticket-1",
        ),
        action_runtime.execute(
            context,
            definition,
            {"customer_ref": "cust-claim", "subject": "Idempotent"},
            idempotency_key="logical-action-ticket-1",
        ),
    )
    assert all(result.status.value == "SUCCEEDED" for result in results)
    async with db_factory() as session:
        tickets = list(
            await session.scalars(
                select(Ticket).where(
                    Ticket.workspace_id == base["workspace_id"],
                    Ticket.idempotency_key == "logical-action-ticket-1",
                )
            )
        )
    assert len(tickets) == 1
