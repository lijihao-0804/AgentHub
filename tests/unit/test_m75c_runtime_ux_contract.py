from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from apps.api.app import create_app
from apps.api.schemas.approvals import ApprovalResponse
from packages.agent_runtime.events import AgentEventEmitter, AgentEventType
from packages.agent_runtime.sse import event_to_sse
from packages.core.config.settings import Settings


def test_run_scoped_approval_route_and_existing_runtime_routes_are_registered() -> None:
    paths = create_app(Settings(testing=True)).openapi()["paths"]

    assert "/api/v1/workspaces/{workspace_id}/agent-runs/{run_id}/approvals" in paths
    assert "/api/v1/workspaces/{workspace_id}/agent-runs/{run_id}" in paths
    assert "/api/v1/workspaces/{workspace_id}/agent-runs/{run_id}/steps" in paths
    assert "/api/v1/workspaces/{workspace_id}/approvals/{approval_id}/approve" in paths
    assert "/api/v1/workspaces/{workspace_id}/approvals/{approval_id}/deny" in paths


def test_approval_response_fields_are_reused_without_a_playground_schema() -> None:
    assert set(ApprovalResponse.model_fields) == {
        "id",
        "workspace_id",
        "run_id",
        "agent_version_id",
        "logical_action_id",
        "tool_revision_id",
        "tool_identity",
        "canonical_arguments",
        "canonical_args_hash",
        "decision_status",
        "execution_status",
        "requested_by",
        "decided_by",
        "decided_at",
        "claimed_at",
        "executed_at",
        "expires_at",
        "failure_code",
        "safe_failure_message",
        "safe_result",
        "execution_attempt_count",
        "created_at",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_sse_envelope_sequence_and_safe_approval_payload_are_frozen() -> None:
    emitter = AgentEventEmitter(
        run_id=str(uuid4()),
        agent_version_id=str(uuid4()),
        request_id="m75c-request",
    )
    started = await emitter.emit(
        AgentEventType.RUN_STARTED,
        {"status": "RUNNING", "model_step_count": 0, "tool_call_count": 0},
    )
    approval = await emitter.emit(
        AgentEventType.APPROVAL_REQUIRED,
        {
            "approval_id": str(uuid4()),
            "logical_action_id": "logical-action-1",
            "tool_identity": "create_ticket",
            "risk_level": "HIGH",
            "decision_status": "PENDING",
            "execution_status": "NOT_STARTED",
        },
    )

    assert started.sequence == 1
    assert approval.sequence == 2
    assert set(started.as_dict()) == {
        "event_id",
        "type",
        "request_id",
        "run_id",
        "step_id",
        "timestamp",
        "payload",
        "sequence",
        "agent_version_id",
    }
    assert set(approval.payload) == {
        "approval_id",
        "logical_action_id",
        "tool_identity",
        "risk_level",
        "decision_status",
        "execution_status",
    }
    frame = event_to_sse(approval)
    payload = json.loads(frame.split("\n", 2)[1][len("data: ") :])
    assert set(payload) == set(started.as_dict())
    assert payload["type"] == "approval.required"

    with pytest.raises(ValueError, match="unsafe event fields"):
        await emitter.emit(
            AgentEventType.APPROVAL_REQUIRED,
            {
                "approval_id": str(uuid4()),
                "logical_action_id": "logical-action-1",
                "tool_identity": "create_ticket",
                "risk_level": "HIGH",
                "decision_status": "PENDING",
                "execution_status": "NOT_STARTED",
                "canonical_arguments": {"customer_ref": "must-not-stream"},
            },
        )


@pytest.mark.asyncio
async def test_event_emitter_is_local_monotonic_order_and_exposes_frozen_types() -> None:
    emitter = AgentEventEmitter(run_id="run-1", agent_version_id="version-1")
    events = await asyncio.gather(
        *(emitter.emit(AgentEventType.MESSAGE_DELTA, {"delta": str(index)}) for index in range(4))
    )
    assert sorted(event.sequence for event in events) == [1, 2, 3, 4]
    assert [event.value for event in AgentEventType] == [
        "run.started",
        "message.started",
        "context.budget",
        "message.delta",
        "message.completed",
        "retrieval.started",
        "retrieval.completed",
        "rerank.completed",
        "tool.requested",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "approval.required",
        "approval.resolved",
        "run.cancel_requested",
        "usage",
        "run.completed",
        "run.failed",
        "run.cancelled",
    ]


def test_run_status_contract_is_provider_neutral() -> None:
    statuses = {
        "RUNNING",
        "WAITING_APPROVAL",
        "SUCCEEDED",
        "FAILED",
        "NEEDS_ATTENTION",
        "CANCEL_REQUESTED",
        "CANCELLED",
    }
    assert "PAUSED" not in statuses
    assert "WAITING_USER" not in statuses
    assert "DONE" not in statuses
    assert "ERROR" not in statuses
