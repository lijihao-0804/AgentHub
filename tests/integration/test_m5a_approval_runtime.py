from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
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
from packages.approvals import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
    ApprovalReconciliationService,
    ApprovalService,
)
from packages.control_plane.models import AuditLog
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionExecutionResult, ActionRegistry, ActionRuntime
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


class RecordingTraceSpan:
    def __init__(self, sink: RecordingTraceSink, name: str) -> None:
        self.sink = sink
        self.name = name

    async def end(self, *, attributes=None, status="ok", failure_code=None) -> None:
        self.sink.ended.append(
            {
                "name": self.name,
                "attributes": dict(attributes or {}),
                "status": status,
                "failure_code": failure_code,
            }
        )


class RecordingTraceSink:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended: list[dict[str, object]] = []

    async def start_span(self, name, attributes=None):
        del attributes
        self.started.append(name)
        return RecordingTraceSpan(self, name)


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
    trace_sink = RecordingTraceSink()
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=adapter,
        trace_sink=trace_sink,
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
        trace_sink=trace_sink,
    )
    resumed = await restarted.resume(admin_context, run_id=first.run_id, approval_id=approval.id)
    assert resumed.run_id == first.run_id
    assert resumed.status == "SUCCEEDED"
    assert "approval.wait" in trace_sink.started
    assert "approval.execute" in trace_sink.started
    for span in trace_sink.ended:
        assert "canonical_arguments" not in span["attributes"]
        assert "credential" not in span["attributes"]

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
    async with db_factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.workspace_id == base["workspace_id"],
                AuditLog.action == "approval.decide",
                AuditLog.resource_id == str(approval.id),
            )
        )
    assert audit is not None
    assert audit.safe_metadata["decision"] == ApprovalDecisionStatus.APPROVED.value
    assert "canonical_arguments" not in audit.safe_metadata


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_winner_and_ticket_is_effectively_once(db_factory) -> None:
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
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


class UnknownOutcomeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context, definition, arguments, *, idempotency_key):
        del context, definition, arguments, idempotency_key
        self.calls += 1
        return ActionExecutionResult.unknown_outcome("SYNTHETIC_EXTERNAL_UNCERTAIN")


@pytest.mark.asyncio
async def test_unknown_outcome_needs_attention_and_resume_does_not_retry(db_factory) -> None:
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
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
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(
            Customer(
                workspace_id=base["workspace_id"], customer_ref="cust-unknown", name="Unknown"
            )
        )
        await session.commit()
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    admin_context = context.model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "tool_run", "workspace_read", "approve_action"}
            )
        }
    )
    gateway = ScriptedApprovalGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    first = await AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=adapter,
    ).run(admin_context, agent_version_id=base["version"].id, input_text="uncertain")
    assert first.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(admin_context))[0]
    await ApprovalService(db_factory).decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    unknown = UnknownOutcomeExecutor()
    action_runtime = ActionRuntime(
        session_factory=db_factory,
        registry=ActionRegistry(
            session_factory=db_factory, overrides={"create_ticket": unknown}
        ),
    )
    restarted = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=action_runtime,
        checkpoint_adapter=adapter,
    )
    result = await restarted.resume(admin_context, run_id=first.run_id, approval_id=approval.id)
    assert result.status == "NEEDS_ATTENTION"
    again = await restarted.resume(admin_context, run_id=first.run_id, approval_id=approval.id)
    assert again.status == "NEEDS_ATTENTION"
    assert unknown.calls == 1
    async with db_factory() as session:
        persisted = await session.get(type(approval), approval.id)
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert persisted is not None
    assert persisted.execution_status == ApprovalExecutionStatus.UNKNOWN_OUTCOME
    assert len(tickets) == 0


@pytest.mark.asyncio
async def test_cancel_waiting_approval_is_terminal_and_not_executable(db_factory) -> None:
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
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
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(
            Customer(
                workspace_id=base["workspace_id"], customer_ref="cust-cancel", name="Cancel"
            )
        )
        await session.commit()
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    gateway = ScriptedApprovalGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=adapter,
    )
    first = await service.run(context, agent_version_id=base["version"].id, input_text="cancel")
    assert first.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(context.model_copy(
        update={"permissions": frozenset({"workspace_read"})}
    )))[0]
    cancelled = await service.cancel(context, run_id=first.run_id)
    assert cancelled.status == "CANCELLED"
    async with db_factory() as session:
        persisted = await session.get(type(approval), approval.id)
    assert persisted is not None
    assert persisted.decision_status == ApprovalDecisionStatus.CANCELLED


class FailIfCalledExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context, definition, arguments, *, idempotency_key):
        del context, definition, arguments, idempotency_key
        self.calls += 1
        raise AssertionError("the already-succeeded action must not execute again")


@pytest.mark.asyncio
async def test_crash_after_action_commit_reuses_result_without_second_side_effect(
    db_factory,
) -> None:
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
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
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(
            Customer(
                workspace_id=base["workspace_id"],
                customer_ref="cust-1",
                name="Crash one",
            )
        )
        await session.commit()
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    admin_context = context.model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "tool_run", "workspace_read", "approve_action"}
            )
        }
    )
    gateway = ScriptedApprovalGateway()
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=adapter,
    )
    first = await service.run(context, agent_version_id=base["version"].id, input_text="crash one")
    assert first.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(admin_context))[0]
    await ApprovalService(db_factory).decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
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
    approvals = ApprovalService(db_factory)
    claimed = await approvals.claim_execution(admin_context, approval.id)
    assert claimed is not None
    action_result = await ActionRuntime(session_factory=db_factory).execute(
        admin_context,
        definition,
        dict(claimed.canonical_arguments),
        idempotency_key=claimed.idempotency_key,
    )
    assert action_result.status.value == "SUCCEEDED"
    await approvals.complete_execution(
        admin_context,
        approval.id,
        status=ApprovalExecutionStatus.SUCCEEDED,
        safe_result=action_result.data,
    )
    fail_if_called = FailIfCalledExecutor()
    restarted = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=approvals,
        action_runtime=ActionRuntime(
            session_factory=db_factory,
            registry=ActionRegistry(
                session_factory=db_factory,
                overrides={"create_ticket": fail_if_called},
            ),
        ),
        checkpoint_adapter=adapter,
    )
    resumed = await restarted.resume(
        admin_context, run_id=first.run_id, approval_id=approval.id
    )
    assert resumed.status == "SUCCEEDED"
    assert fail_if_called.calls == 0
    async with db_factory() as session:
        tickets = list(
            await session.scalars(
                select(Ticket).where(Ticket.workspace_id == base["workspace_id"])
            )
        )
    assert len(tickets) == 1


@pytest.mark.asyncio
async def test_approval_without_checkpoint_reconciles_once_to_needs_attention(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex)
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            input_text="missing checkpoint",
            created_by=base["user"].id,
        )
        session.add(run)
        await session.commit()
    approvals = ApprovalService(db_factory)
    approval = await approvals.create_or_get(
        base["context"],
        run_id=run.id,
        agent_version_id=base["version"].id,
        tool_revision_id=None,
        tool_identity="create_ticket",
        arguments={},
        input_schema={"type": "object", "additionalProperties": False},
        proposal_ordinal=0,
    )
    reconciler = ApprovalReconciliationService(db_factory)
    first = await reconciler.reconcile_run(
        base["context"], run.id, checkpoint_exists=False
    )
    second = await reconciler.reconcile_run(
        base["context"], run.id, checkpoint_exists=False
    )
    assert first.status == "NEEDS_ATTENTION"
    assert second.status == "NEEDS_ATTENTION"
    assert second.failure_code == "APPROVAL_CHECKPOINT_MISSING"
    listed = await approvals.list(
        base["context"].model_copy(update={"permissions": frozenset({"workspace_read"})})
    )
    assert [item.id for item in listed] == [approval.id]


@pytest.mark.asyncio
async def test_durable_checkpoint_with_stale_running_status_reconciles_to_waiting(
    db_factory,
) -> None:
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
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
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "tool_run"})}
    )
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: ScriptedApprovalGateway(),
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )
    first = await service.run(context, agent_version_id=base["version"].id, input_text="reconcile")
    assert first.status == "WAITING_APPROVAL"
    async with db_factory() as session:
        run = await session.get(AgentRun, first.run_id)
        assert run is not None
        run.status = "RUNNING"
        await session.commit()
    reconciler = ApprovalReconciliationService(db_factory)
    restored = await reconciler.reconcile_run(context, first.run_id, checkpoint_exists=True)
    repeated = await reconciler.reconcile_run(context, first.run_id, checkpoint_exists=True)
    assert restored.status == "WAITING_APPROVAL"
    assert repeated.status == "WAITING_APPROVAL"
    assert repeated.failure_code is None


@pytest.mark.asyncio
async def test_expired_approval_and_developer_decision_are_fail_closed(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex)
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            input_text="expiry",
            created_by=base["user"].id,
        )
        session.add(run)
        await session.commit()
    approvals = ApprovalService(db_factory)
    approval = await approvals.create_or_get(
        base["context"],
        run_id=run.id,
        agent_version_id=base["version"].id,
        tool_revision_id=None,
        tool_identity="create_ticket",
        arguments={},
        input_schema={"type": "object", "additionalProperties": False},
        proposal_ordinal=0,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(AgentHubError, match="permission") as denied:
        await approvals.decide(
            base["context"], approval.id, decision=ApprovalDecisionStatus.APPROVED
        )
    assert denied.value.code == "FORBIDDEN"
    admin_context = base["context"].model_copy(
        update={
            "permissions": frozenset(
                {"agent_run", "workspace_read", "approve_action"}
            )
        }
    )
    expired = await approvals.decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    assert expired.decision_status == ApprovalDecisionStatus.EXPIRED
