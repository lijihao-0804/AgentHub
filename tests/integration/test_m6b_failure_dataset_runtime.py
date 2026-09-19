from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benchmarks.observability.schema import load_dataset
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun, RunStep
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import (
    Approval,
    ApprovalDecisionStatus,
    ApprovalReconciliationService,
    ApprovalService,
)
from packages.control_plane.models import AuditLog
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.model_gateway.contracts import (
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.tools.actions import ActionExecutionResult, ActionRegistry, ActionRuntime
from packages.tools.audit import SqlAlchemyToolAuditSink
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from tests.integration.test_m4c_agent_runtime import (
    ScriptedGateway,
    _calculator_spec,
    _seed,
)
from tests.integration.test_m5a_approval_runtime import ScriptedApprovalGateway

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M6-B runtime dataset tests.",
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


_WRITE_TOOL_SPEC: dict[str, object] = {
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

_SEARCH_TOOL_SPEC: dict[str, object] = {
    "kind": "builtin",
    "identity": "search_knowledge",
    "description": "Search the published knowledge snapshot.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query", "limit"],
        "additionalProperties": False,
    },
    "effect": "READ",
    "risk_level": "LOW",
    "approval_policy": "NEVER",
}


class _FailingModelGateway:
    async def generate_resolved(self, context, plan, request):
        del context, plan, request
        raise ModelGatewayError(
            ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
            retryable=False,
        )


class _InterruptedStreamGateway:
    async def generate_resolved(self, context, plan, request):
        del context, plan, request
        return ModelResponse(content="unused", provider="fake", model="fake")

    def stream_resolved(self, context, plan, request):
        del context, plan, request

        async def events():
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta="visible synthetic token",
            )
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED,
                retryable=False,
            )

        return events()


class _FailingActionExecutor:
    async def execute(self, context, definition, arguments, *, idempotency_key):
        del context, definition, arguments, idempotency_key
        return ActionExecutionResult.failed(
            "ACTION_EXECUTION_FAILED", "The synthetic action failed."
        )


class _UnknownOutcomeExecutor:
    async def execute(self, context, definition, arguments, *, idempotency_key):
        del context, definition, arguments, idempotency_key
        return ActionExecutionResult.unknown_outcome("SYNTHETIC_EXTERNAL_UNCERTAIN")


async def _read_runtime_evidence(
    db_factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> dict[str, Any]:
    async with db_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        steps = list(
            await session.scalars(
                select(RunStep)
                .where(RunStep.workspace_id == run.workspace_id, RunStep.agent_run_id == run.id)
                .order_by(RunStep.sequence_number)
            )
        )
        audits = list(
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.workspace_id == run.workspace_id,
                    AuditLog.action == "tool.execute",
                    AuditLog.resource_id == str(run.agent_version_id),
                )
                .order_by(AuditLog.created_at)
            )
        )
        approvals = list(
            await session.scalars(
                select(Approval)
                .where(Approval.workspace_id == run.workspace_id, Approval.run_id == run.id)
                .order_by(Approval.created_at)
            )
        )

    return {
        "run_status": run.status,
        "run_failure_code": run.failure_code,
        "step_kinds": [step.kind for step in steps],
        "step_failure_codes": [
            step.safe_metadata.get("error_code")
            for step in steps
            if step.safe_metadata.get("error_code")
        ],
        "tool_failure_codes": [
            audit.safe_metadata.get("error_code")
            for audit in audits
            if audit.safe_metadata.get("error_code")
        ],
        "approval_decisions": [str(approval.decision_status) for approval in approvals],
        "approval_execution_statuses": [
            str(approval.execution_status) for approval in approvals
        ],
    }


async def _start_waiting_approval(
    db_factory: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, object], AgentRunService, object, object]:
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=_WRITE_TOOL_SPEC)
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
    result = await service.run(context, agent_version_id=base["version"].id, input_text="synthetic")
    assert result.status == "WAITING_APPROVAL"
    approval = (await ApprovalService(db_factory).list(admin_context))[0]
    return base, service, admin_context, approval


@pytest.mark.asyncio
async def test_m6b_failure_dataset_is_generated_by_real_runtime(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    dataset = load_dataset(Path("benchmarks/observability/dataset.json"))
    expected_ids = {case.case_id for case in dataset.cases}
    results: dict[str, dict[str, Any]] = {}

    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex)
    model_failure = await AgentRunService(
        db_factory, model_gateway_factory=lambda session: _FailingModelGateway()
    ).run(base["context"], agent_version_id=base["version"].id, input_text="synthetic")
    assert model_failure.status == "FAILED"
    assert model_failure.failure_code == "MODEL_PROVIDER_UNAVAILABLE"
    results["model-failure-before-token"] = await _read_runtime_evidence(
        db_factory, model_failure.run_id
    )

    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex)
        version = base["version"]
        resolved = dict(version.resolved_spec)
        model = dict(resolved["model"])
        capabilities = dict(model["capabilities"])
        capabilities["streaming"] = True
        model["capabilities"] = capabilities
        resolved["model"] = model
        version.resolved_spec = resolved
        from packages.core.canonical.json_hash import canonical_json_hash

        version.resolved_spec_hash = canonical_json_hash(resolved)
        await session.commit()
    stream = AgentRunService(
        db_factory, model_gateway_factory=lambda session: _InterruptedStreamGateway()
    ).stream(base["context"], agent_version_id=base["version"].id, input_text="synthetic")
    events = [event async for event in stream]
    stream_run_id = UUID(events[0].run_id)
    stream_evidence = await _read_runtime_evidence(db_factory, stream_run_id)
    assert stream_evidence["run_status"] == "FAILED"
    assert stream_evidence["run_failure_code"] == "MODEL_STREAM_INTERRUPTED"
    results["stream-interrupted-after-visible-token"] = stream_evidence

    async def failing_calculator(context, definition, arguments, session_factory):
        del context, definition, arguments, session_factory
        raise RuntimeError("synthetic tool failure")

    async with db_factory() as session:
        base = await _seed(
            session,
            label=uuid4().hex,
            tool_spec=_calculator_spec(),
        )
    tool_gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="frozen-model-a",
                tool_calls=(ModelToolCall("calculator", {"expression": "1 + 1"}),),
            ),
            ModelResponse(content="recovered", provider="fake", model="frozen-model-a"),
        ]
    )
    tool_runtime = ToolRuntime(
        session_factory=db_factory,
        registry=ToolRegistry(handler_overrides={"calculator": failing_calculator}),
        audit_sink=SqlAlchemyToolAuditSink(db_factory),
    )
    tool_result = await AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: tool_gateway,
        tool_runtime=tool_runtime,
    ).run(base["context"], agent_version_id=base["version"].id, input_text="synthetic")
    assert tool_result.status == "SUCCEEDED"
    tool_evidence = await _read_runtime_evidence(db_factory, tool_result.run_id)
    assert "TOOL_EXECUTION_FAILED" in tool_evidence["tool_failure_codes"]
    results["tool-execution-failure"] = tool_evidence

    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=_SEARCH_TOOL_SPEC)
    knowledge_gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="frozen-model-a",
                tool_calls=(ModelToolCall("search_knowledge", {"query": "synthetic", "limit": 1}),),
            ),
            ModelResponse(content="no result", provider="fake", model="frozen-model-a"),
        ]
    )
    knowledge_runtime = ToolRuntime(
        session_factory=db_factory,
        audit_sink=SqlAlchemyToolAuditSink(db_factory),
    )
    knowledge_result = await AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: knowledge_gateway,
        tool_runtime=knowledge_runtime,
    ).run(base["context"], agent_version_id=base["version"].id, input_text="synthetic")
    assert knowledge_result.status == "SUCCEEDED"
    knowledge_evidence = await _read_runtime_evidence(db_factory, knowledge_result.run_id)
    assert "TOOL_EXECUTION_FAILED" in knowledge_evidence["tool_failure_codes"]
    results["knowledge-unavailable"] = knowledge_evidence

    async with db_factory() as session:
        base = await _seed(
            session,
            label=uuid4().hex,
            tool_spec=_calculator_spec(),
            runtime={"max_steps": 4, "max_identical_calls": 2},
        )
    repeated = ModelResponse(
        content="",
        provider="fake",
        model="frozen-model-a",
        tool_calls=(ModelToolCall("calculator", {"expression": "1"}),),
    )
    loop_result = await AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: ScriptedGateway([repeated]),
    ).run(base["context"], agent_version_id=base["version"].id, input_text="synthetic")
    assert loop_result.failure_code == "AGENT_IDENTICAL_TOOL_CALL_LIMIT"
    results["loop-guard"] = await _read_runtime_evidence(db_factory, loop_result.run_id)

    base, service, admin_context, approval = await _start_waiting_approval(db_factory)
    await ApprovalService(db_factory).decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.DENIED
    )
    denied = await service.resume(
        admin_context, run_id=approval.run_id, approval_id=approval.id
    )
    results["approval-denied"] = await _read_runtime_evidence(db_factory, denied.run_id)
    assert results["approval-denied"]["approval_decisions"] == ["DENIED"]

    base, service, admin_context, approval = await _start_waiting_approval(db_factory)
    await ApprovalService(db_factory).decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    failing_action = ActionRuntime(
        session_factory=db_factory,
        registry=ActionRegistry(
            session_factory=db_factory,
            overrides={"create_ticket": _FailingActionExecutor()},
        ),
    )
    service.action_runtime = failing_action
    failed_action_result = await service.resume(
        admin_context, run_id=approval.run_id, approval_id=approval.id
    )
    action_evidence = await _read_runtime_evidence(db_factory, failed_action_result.run_id)
    assert action_evidence["approval_execution_statuses"] == ["FAILED"]
    results["action-execution-failed"] = action_evidence

    base, service, admin_context, approval = await _start_waiting_approval(db_factory)
    await ApprovalService(db_factory).decide(
        admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    unknown_action = ActionRuntime(
        session_factory=db_factory,
        registry=ActionRegistry(
            session_factory=db_factory,
            overrides={"create_ticket": _UnknownOutcomeExecutor()},
        ),
    )
    service.action_runtime = unknown_action
    unknown_result = await service.resume(
        admin_context, run_id=approval.run_id, approval_id=approval.id
    )
    assert unknown_result.status == "NEEDS_ATTENTION"
    results["unknown-outcome"] = await _read_runtime_evidence(db_factory, unknown_result.run_id)

    base, service, context, approval = await _start_waiting_approval(db_factory)
    del service, approval
    # Use the only waiting Run in this freshly seeded workspace.
    async with db_factory() as session:
        waiting_run = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.workspace_id == base["workspace_id"],
                AgentRun.status == "WAITING_APPROVAL",
            )
            .order_by(AgentRun.created_at.desc())
        )
    assert waiting_run is not None
    reconciled = await ApprovalReconciliationService(db_factory).reconcile_run(
        context, waiting_run.id, checkpoint_exists=False
    )
    assert reconciled.status == "NEEDS_ATTENTION"
    results["checkpoint-missing"] = await _read_runtime_evidence(db_factory, waiting_run.id)

    base, service, context, approval = await _start_waiting_approval(db_factory)
    del approval
    cancelled = await service.cancel(
        context, run_id=await _latest_run(db_factory, base["workspace_id"])
    )
    assert cancelled.status == "CANCELLED"
    results["cancelled-run"] = await _read_runtime_evidence(db_factory, cancelled.run_id)

    assert set(results) == expected_ids
    assert len(results) == 10
    assert all(
        "run_status" in evidence
        and "step_kinds" in evidence
        and "tool_failure_codes" in evidence
        for evidence in results.values()
    )
    assert all(
        "input_text" not in evidence
        and "final_output" not in evidence
        and "canonical_arguments" not in evidence
        for evidence in results.values()
    )


async def _latest_run(
    db_factory: async_sessionmaker[AsyncSession], workspace_id: UUID
) -> UUID:
    async with db_factory() as session:
        run = await session.scalar(
            select(AgentRun)
            .where(AgentRun.workspace_id == workspace_id)
            .order_by(AgentRun.created_at.desc())
        )
    assert run is not None
    return run.id
