from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import packages.agent_runtime.runtime_graph as runtime_graph_module
from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextBudgetConfig,
    ContextBudgetPolicy,
)
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_runtime.runtime_projection import (
    _RUNTIME_POLICY,
    _approval_tool_result,
    _bounded_tool_result,
    _categorize_messages,
)
from packages.approvals.contracts import ApprovalDecisionStatus, ApprovalExecutionStatus
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.tools.actions import ActionExecutionResult
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolResultStatus,
    ToolRisk,
)


def _approval(
    decision: str,
    execution: str,
    *,
    run_id=None,
    agent_version_id=None,
    tool_revision_id=None,
    safe_result: dict[str, object] | None = None,
    failure_code: str | None = None,
    safe_failure_message: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        run_id=run_id or uuid4(),
        agent_version_id=agent_version_id or uuid4(),
        logical_action_id="logical-action",
        tool_revision_id=tool_revision_id,
        tool_identity="create_ticket",
        decision_status=decision,
        execution_status=execution,
        safe_result=safe_result,
        failure_code=failure_code,
        safe_failure_message=safe_failure_message,
    )


def _definition() -> ToolDefinition:
    return ToolDefinition(
        identity="create_ticket",
        revision_id=uuid4(),
        spec_hash="spec-hash",
        description="Create a ticket",
        input_schema={"type": "object"},
        effect=ToolEffect.WRITE,
        risk_level=ToolRisk.HIGH,
        approval_policy=ToolApprovalPolicy.ALWAYS,
        timeout_seconds=30,
    )


def _graph() -> _AgentRunGraph:
    graph = object.__new__(_AgentRunGraph)
    graph.sequence = 0
    graph.mode = "sync"
    graph.context = SimpleNamespace(
        workspace_id=str(uuid4()),
        permissions={"agent_run", "workspace_read", "tool_run"},
    )
    graph.run = SimpleNamespace(id=uuid4(), agent_version_id=uuid4(), thread_id=None)
    graph._runtime_context_budget = {
        "reserved_output_tokens": 1,
        "max_retrieval_tokens": 5_000,
        "max_tool_result_tokens": 4_000,
    }

    async def step(*_args, **_kwargs):
        return None

    async def stop_if_requested():
        return None

    async def emit(*_args, **_kwargs):
        return None

    graph.step = step
    graph._stop_if_requested = stop_if_requested
    graph._emit = emit
    return graph


def _action_graph(approval: SimpleNamespace, action_result: ActionExecutionResult):
    graph = _graph()
    completed: dict[str, object] = {}
    calls = {"claimed": 0, "executed": 0, "completed": 0}

    class ApprovalService:
        async def claim_execution(self, _context, approval_id):
            calls["claimed"] += 1
            return SimpleNamespace(
                canonical_arguments={"subject": "safe"},
                idempotency_key="idempotency-key",
            )

        async def complete_execution(self, _context, _approval_id, *, status, **kwargs):
            calls["completed"] += 1
            completed.update(status=status, **kwargs)
            return _approval(
                ApprovalDecisionStatus.APPROVED.value,
                status.value,
                safe_result=kwargs.get("safe_result"),
                failure_code=kwargs.get("failure_code"),
                safe_failure_message=kwargs.get("safe_failure_message"),
            )

        async def get(self, _context, _approval_id):
            raise AssertionError("terminal replay must not claim or recover")

    class ActionRuntime:
        async def execute(self, *_args, **_kwargs):
            calls["executed"] += 1
            return action_result

    async def load_resume_approval(_context, _run, _approval_id):
        return approval

    async def authoritative_context(context, _run):
        return context

    graph.service = SimpleNamespace(
        action_runtime=ActionRuntime(),
        approval_service=ApprovalService(),
        trace_sink=None,
        _load_resume_approval=load_resume_approval,
        _authoritative_run_context=authoritative_context,
    )
    graph._action_permissions_intact = lambda _definition: True
    return graph, calls, completed


def _action_state() -> dict:
    return {
        "tool_definitions": {"create_ticket": _definition()},
        "action_calls": [
            {
                "call": {
                    "name": "create_ticket",
                    "tool_call_id": "call-1",
                    "arguments": {"subject": "safe"},
                },
                "approval_id": str(uuid4()),
            }
        ],
    }


def _patch_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start_span(*_args, **_kwargs):
        return object()

    async def end_span(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime_graph_module, "_safe_start_span", start_span)
    monkeypatch.setattr(runtime_graph_module, "_safe_end_span", end_span)


async def _observe_result(
    result: ToolResult, *, max_tool_result_tokens: int = 4_000
) -> tuple[dict, dict]:
    graph = _graph()
    graph.service = SimpleNamespace()
    graph._runtime_context_budget["max_tool_result_tokens"] = max_tool_result_tokens
    definition = _definition()
    call_id = "call-1"
    state = {
        "messages": [
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(
                        name=definition.identity,
                        arguments={},
                        provider_tool_call_id=call_id,
                    ),
                ),
            )
        ],
        "proposed_tool_calls": [
            {"name": definition.identity, "tool_call_id": call_id, "arguments": {}}
        ],
        "executed_observations": {call_id: result},
        "runtime": {"context_budget": graph._runtime_context_budget},
        "tool_definitions": {definition.identity: definition},
    }
    update = await graph.observation(state)
    payload = json.loads(update["messages"][-1].content)
    return update, payload


def _admission_policy(max_tool_result_tokens: int) -> ContextBudgetPolicy:
    profile = SimpleNamespace(
        max_tokens=1,
        capabilities=SimpleNamespace(max_context_tokens=10_000),
    )
    return ContextBudgetPolicy(
        profiles=(profile,),
        config=ContextBudgetConfig(
            reserved_output_tokens=1,
            max_retrieval_tokens=100,
            max_tool_result_tokens=max_tool_result_tokens,
        ),
        estimator=ApproxTokenEstimator(),
    )


def test_existing_tool_results_keep_no_approval_facts_and_facts_are_paired() -> None:
    success = ToolResult.success({"value": 1})
    failure = ToolResult.failure("FAILED", "failed")

    assert success.approval_decision is None
    assert success.approval_execution is None
    assert failure.approval_decision is None
    assert failure.approval_execution is None
    with pytest.raises(ValueError, match="provided together"):
        ToolResult(
            status=ToolResultStatus.SUCCESS,
            approval_decision="APPROVED",
        )


@pytest.mark.parametrize(
    ("execution", "safe_result", "failure_code", "expected_status"),
    [
        (ApprovalExecutionStatus.SUCCEEDED, {"ticket_id": "ticket-1"}, None, "SUCCESS"),
        (ApprovalExecutionStatus.FAILED, None, "TICKET_CREATE_FAILED", "ERROR"),
        (ApprovalExecutionStatus.UNKNOWN_OUTCOME, None, "ACTION_OUTCOME_UNKNOWN", "ERROR"),
    ],
)
def test_approval_result_projects_both_real_state_dimensions(
    execution: ApprovalExecutionStatus,
    safe_result: dict[str, object] | None,
    failure_code: str | None,
    expected_status: str,
) -> None:
    result = _approval_tool_result(
        _approval(
            ApprovalDecisionStatus.APPROVED.value,
            execution.value,
            safe_result=safe_result,
            failure_code=failure_code,
            safe_failure_message="safe failure" if failure_code else None,
        )
    )

    assert result.status.value == expected_status
    assert result.approval_decision == "APPROVED"
    assert result.approval_execution == execution.value


@pytest.mark.asyncio
async def test_denied_action_exposes_decision_and_not_started_execution(monkeypatch) -> None:
    graph = _graph()
    graph.service = SimpleNamespace(
        checkpoint_adapter=object(),
        trace_sink=None,
        approval_service=SimpleNamespace(),
    )
    pending = _approval(
        ApprovalDecisionStatus.PENDING.value,
        ApprovalExecutionStatus.NOT_STARTED.value,
    )
    definition = _definition()
    denied = _approval(
        ApprovalDecisionStatus.DENIED.value,
        ApprovalExecutionStatus.NOT_STARTED.value,
        run_id=graph.run.id,
        agent_version_id=graph.run.agent_version_id,
        tool_revision_id=definition.revision_id,
    )

    async def create_or_get(*_args, **_kwargs):
        return pending

    async def load_resume_approval(_context, _run, _approval_id):
        return denied

    graph.service.approval_service.create_or_get = create_or_get
    graph.service._load_resume_approval = load_resume_approval
    monkeypatch.setattr(runtime_graph_module, "approval_interrupt", lambda payload: payload)

    state = {
        "tool_definitions": {"create_ticket": definition},
        "pending_tool_calls": [
            {
                "name": "create_ticket",
                "arguments": {"subject": "safe"},
                "tool_call_id": "call-1",
                "invalid_code": None,
            }
        ],
        "model_round_count": 0,
    }
    update = await graph.policy(state)
    result = update["pre_observations"]["call-1"]

    assert result.error_code == "TOOL_APPROVAL_DENIED"
    assert result.approval_decision == "DENIED"
    assert result.approval_execution == "NOT_STARTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution",
    [
        ApprovalExecutionStatus.SUCCEEDED.value,
        ApprovalExecutionStatus.FAILED.value,
        ApprovalExecutionStatus.UNKNOWN_OUTCOME.value,
    ],
)
async def test_persisted_terminal_replay_exposes_facts_without_execution(
    monkeypatch, execution: str
) -> None:
    _patch_spans(monkeypatch)
    approval = _approval(
        ApprovalDecisionStatus.APPROVED.value,
        execution,
        safe_result={"ticket_id": "ticket-1"} if execution == "SUCCEEDED" else None,
        failure_code="ACTION_FAILED" if execution != "SUCCEEDED" else None,
        safe_failure_message="safe failure" if execution != "SUCCEEDED" else None,
    )
    graph, calls, _ = _action_graph(approval, ActionExecutionResult.succeeded({}))

    update = await graph.action_execute(_action_state())
    result = update["executed_observations"]["call-1"]

    assert calls == {"claimed": 0, "executed": 0, "completed": 0}
    assert result.approval_decision == "APPROVED"
    assert result.approval_execution == execution
    if execution == "UNKNOWN_OUTCOME":
        assert update["run_status"] == "NEEDS_ATTENTION"
        assert update["failure_code"] == "ACTION_RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_result", "execution", "status"),
    [
        (
            ActionExecutionResult.succeeded({"ticket_id": "ticket-1"}),
            ApprovalExecutionStatus.SUCCEEDED.value,
            ToolResultStatus.SUCCESS,
        ),
        (
            ActionExecutionResult.failed("TICKET_CREATE_FAILED", "safe failure"),
            ApprovalExecutionStatus.FAILED.value,
            ToolResultStatus.ERROR,
        ),
    ],
)
async def test_fresh_execution_uses_server_completion_for_model_facts(
    monkeypatch,
    action_result: ActionExecutionResult,
    execution: str,
    status: ToolResultStatus,
) -> None:
    _patch_spans(monkeypatch)
    approval = _approval(
        ApprovalDecisionStatus.APPROVED.value,
        ApprovalExecutionStatus.NOT_STARTED.value,
    )
    graph, calls, completed = _action_graph(approval, action_result)

    update = await graph.action_execute(_action_state())
    result = update["executed_observations"]["call-1"]

    assert calls == {"claimed": 1, "executed": 1, "completed": 1}
    assert completed["status"].value == execution
    assert result.status is status
    assert result.approval_decision == "APPROVED"
    assert result.approval_execution == execution


@pytest.mark.asyncio
async def test_unknown_fresh_execution_is_terminal_and_not_retried(monkeypatch) -> None:
    _patch_spans(monkeypatch)
    approval = _approval(
        ApprovalDecisionStatus.APPROVED.value,
        ApprovalExecutionStatus.NOT_STARTED.value,
    )
    graph, calls, completed = _action_graph(
        approval, ActionExecutionResult.unknown_outcome("PROVIDER_OUTCOME_UNKNOWN")
    )

    update = await graph.action_execute(_action_state())
    result = update["executed_observations"]["call-1"]

    assert calls == {"claimed": 1, "executed": 1, "completed": 1}
    assert completed["status"] is ApprovalExecutionStatus.UNKNOWN_OUTCOME
    assert result.approval_decision == "APPROVED"
    assert result.approval_execution == "UNKNOWN_OUTCOME"
    assert update["run_status"] == "NEEDS_ATTENTION"
    assert update["failure_code"] == "ACTION_RECONCILIATION_REQUIRED"


def test_bounded_projection_preserves_facts_in_success_failure_and_truncation() -> None:
    facts = {"approval_decision": "APPROVED", "approval_execution": "SUCCEEDED"}
    plain = _bounded_tool_result(
        ToolResult.success({"approval_decision": "APPROVED", "approval_execution": "SUCCEEDED"})
    )
    success = ToolResult.success({"payload": "x" * 20_000}, **facts)
    failure = ToolResult.failure(
        "ACTION_FAILED",
        "safe failure " * 1_000,
        approval_decision="APPROVED",
        approval_execution="FAILED",
    )

    normal = _bounded_tool_result(
        ToolResult.success({"ok": True}, **facts), max_tool_result_tokens=100
    )
    truncated_success = _bounded_tool_result(success, max_tool_result_tokens=100)
    truncated_failure = _bounded_tool_result(failure, max_tool_result_tokens=100)

    assert "approval_decision" not in plain
    assert "approval_execution" not in plain
    assert plain["data"]["approval_decision"] == "APPROVED"
    assert plain["data"]["approval_execution"] == "SUCCEEDED"
    assert normal["approval_decision"] == "APPROVED"
    assert normal["approval_execution"] == "SUCCEEDED"
    assert truncated_success["truncated"] is True
    assert truncated_success["approval_decision"] == "APPROVED"
    assert truncated_success["approval_execution"] == "SUCCEEDED"
    assert truncated_failure["truncated"] is True
    assert truncated_failure["approval_decision"] == "APPROVED"
    assert truncated_failure["approval_execution"] == "FAILED"


@pytest.mark.asyncio
async def test_graph_observation_exposes_only_framework_facts_and_not_data_promotion() -> None:
    result = ToolResult.success(
        {
            "approval_decision": "APPROVED",
            "approval_execution": "SUCCEEDED",
            "ticket_id": "ticket-1",
        },
        approval_decision="DENIED",
        approval_execution="NOT_STARTED",
    )
    _, payload = await _observe_result(result)

    assert payload["approval_decision"] == "DENIED"
    assert payload["approval_execution"] == "NOT_STARTED"
    assert payload["data"]["approval_decision"] == "APPROVED"
    assert payload["data"]["approval_execution"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_context_budget_second_projection_keeps_approval_facts_atomic() -> None:
    result = ToolResult.success(
        {"payload": "x" * 20_000},
        approval_decision="APPROVED",
        approval_execution="SUCCEEDED",
    )
    update, observed = await _observe_result(result, max_tool_result_tokens=1_000)
    assert observed["approval_decision"] == "APPROVED"
    assert observed["approval_execution"] == "SUCCEEDED"
    categorized = _categorize_messages(update["messages"])
    estimator = ApproxTokenEstimator()

    admitted = _admission_policy(160).admit(categorized)
    tool_message = next(message for message in admitted.messages if message.role == "tool")
    projected = json.loads(tool_message.content)
    assert projected["approval_decision"] == "APPROVED"
    assert projected["approval_execution"] == "SUCCEEDED"
    assert projected["truncated"] is True
    assert estimator.estimate(tool_message.content) <= 160

    tiny = _admission_policy(24).admit(categorized)
    tiny_tools = [message for message in tiny.messages if message.role == "tool"]
    assert tiny_tools
    tiny_payload = json.loads(tiny_tools[0].content)
    assert "approval_decision" not in tiny_payload
    assert "approval_execution" not in tiny_payload
    assert tiny_payload["truncated"] is True
    assert estimator.estimate(tiny_tools[0].content) <= 24


@pytest.mark.asyncio
async def test_context_budget_second_projection_keeps_legacy_tool_shape() -> None:
    update, observed = await _observe_result(ToolResult.success({"ok": True}))
    assert "approval_decision" not in observed
    assert "approval_execution" not in observed

    admitted = _admission_policy(1_000).admit(_categorize_messages(update["messages"]))
    tool_message = next(message for message in admitted.messages if message.role == "tool")

    assert json.loads(tool_message.content) == observed


def test_runtime_policy_distinguishes_no_approval_tool_from_no_approval_process() -> None:
    assert "no approval tool" in _RUNTIME_POLICY
    assert "cannot self-approve" in _RUNTIME_POLICY
    assert "approval workflows may still exist" in _RUNTIME_POLICY
    assert "Approval decision and action execution are separate facts" in _RUNTIME_POLICY
    assert "Approval tools are unavailable" not in _RUNTIME_POLICY
