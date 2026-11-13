"""AE-Real-2: unified execution evidence and safety-boundary contracts."""

from __future__ import annotations

import pytest

from packages.agent_runtime.runtime_projection import _safe_step_metadata
from packages.evaluation.metrics import MetricStatus
from packages.evaluation.trajectory import build_trajectory_observation, evaluate_trajectory


def _approval_case(
    decision: str = "APPROVED",
    *,
    execute_action: bool = True,
    action_status: str = "SUCCEEDED",
):
    steps = [
        {
            "index": 0,
            "kind": "TOOL_PROPOSAL",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "arguments_hashes": ["h-query"],
        },
        {
            "index": 1,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "arguments_hashes": ["h-query"],
            "tool_results": [
                {"tool_identity": "query_customer", "status": "SUCCESS", "error_code": None}
            ],
        },
        {
            "index": 2,
            "kind": "TOOL_PROPOSAL",
            "status": "SUCCEEDED",
            "tool_identities": ["create_ticket"],
            "arguments_hashes": ["h-ticket"],
        },
        {"index": 3, "kind": "APPROVAL_WAIT", "status": "WAITING"},
    ]
    if execute_action:
        steps.append(
            {
                "index": 4,
                "kind": "ACTION",
                "status": "FAILED" if action_status == "FAILED" else "SUCCEEDED",
                "tool_identities": ["create_ticket"],
                "logical_action_ids": ["logical-1"],
                "action_results": [
                    {
                        "logical_action_id": "logical-1",
                        "tool_identity": "create_ticket",
                        "status": action_status,
                        "error_code": "ACTION_FAILED" if action_status == "FAILED" else None,
                    }
                ],
            }
        )
    return steps, [
        {
            "approval_id": "approval-1",
            "logical_action_id": "logical-1",
            "tool_identity": "create_ticket",
            "arguments_hash": "h-ticket",
            "decision_status": decision,
            "execution_status": action_status if execute_action else "NOT_STARTED",
        }
    ]


def test_action_execution_is_part_of_executed_calls_and_sequence() -> None:
    steps, approvals = _approval_case("APPROVED")
    observation = build_trajectory_observation(
        steps=steps,
        approvals=approvals,
        terminal={"status": "SUCCEEDED"},
    )

    assert observation["executed_tool_sequence"] == ["query_customer", "create_ticket"]
    assert observation["action_calls"][0]["logical_action_id"] == "logical-1"
    assert observation["action_calls"][0]["arguments_hash"] == "h-ticket"
    assert observation["action_executed"] is True
    assert any(item["kind"] == "ACTION" for item in observation["execution_evidence"])

    metrics = evaluate_trajectory(
        {"required_tools": ["query_customer", "create_ticket"]}, observation
    )
    assert metrics["executed_tool_recall"].value == 1
    assert metrics["trajectory_success"].value == 1


def test_denied_action_is_not_an_executed_call() -> None:
    steps, approvals = _approval_case("DENIED", execute_action=False)
    observation = build_trajectory_observation(
        steps=steps,
        approvals=approvals,
        terminal={"status": "SUCCEEDED"},
    )

    assert observation["executed_tool_sequence"] == ["query_customer"]
    assert observation["action_executed"] is False
    assert observation["unauthorized_execution"] is False
    assert observation["duplicate_side_effect"] is False

    metrics = evaluate_trajectory({"decision": "DENIED"}, observation)
    assert metrics["approval_respected"].value == 1
    assert metrics["trajectory_success"].value == 1


def test_executed_write_without_approved_decision_is_unauthorized() -> None:
    steps, approvals = _approval_case("DENIED", execute_action=True)
    observation = build_trajectory_observation(
        steps=steps,
        approvals=approvals,
        terminal={"status": "SUCCEEDED"},
    )

    assert observation["unauthorized_execution"] is True

    metrics = evaluate_trajectory({"decision": "DENIED"}, observation)
    assert metrics["approval_respected"].value == 0
    assert metrics["trajectory_success"].value == 0


def test_duplicate_action_is_detected_by_logical_action_identity() -> None:
    steps, approvals = _approval_case("APPROVED")
    steps.append(
        {
            "index": 5,
            "kind": "ACTION",
            "status": "SUCCEEDED",
            "tool_identities": ["create_ticket"],
            "logical_action_ids": ["logical-1"],
            "action_results": [
                {
                    "logical_action_id": "logical-1",
                    "tool_identity": "create_ticket",
                    "status": "SUCCEEDED",
                    "error_code": None,
                }
            ],
        }
    )
    observation = build_trajectory_observation(
        steps=steps,
        approvals=approvals,
        terminal={"status": "SUCCEEDED"},
    )

    assert observation["duplicate_side_effect"] is True
    metrics = evaluate_trajectory({}, observation)
    assert metrics["redundant_call_rate"].value == pytest.approx(1 / 3)


def test_unknown_action_outcome_is_not_recovery_and_not_success() -> None:
    steps, approvals = _approval_case("APPROVED", action_status="UNKNOWN_OUTCOME")
    observation = build_trajectory_observation(
        steps=steps,
        approvals=approvals,
        terminal={"status": "NEEDS_ATTENTION"},
    )

    assert observation["unknown_outcome_semantics_ok"] is True
    metrics = evaluate_trajectory({}, observation)
    assert metrics["recovery_rate"].status == MetricStatus.NOT_AVAILABLE
    assert metrics["recovery_rate"].reason == "unknown_outcome_not_retryable"
    assert metrics["trajectory_success"].status == MetricStatus.NOT_AVAILABLE
    assert metrics["trajectory_success"].reason == "unknown_outcome"


def test_recovery_is_based_on_per_call_results_not_model_text() -> None:
    steps = [
        {
            "index": 0,
            "kind": "TOOL_PROPOSAL",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "arguments_hashes": ["h-query"],
        },
        {
            "index": 1,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "tool_results": [
                {
                    "tool_identity": "query_customer",
                    "status": "FAILED",
                    "error_code": "TIMEOUT",
                }
            ],
        },
        {
            "index": 2,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "tool_results": [
                {"tool_identity": "query_customer", "status": "SUCCESS", "error_code": None}
            ],
        },
    ]
    observation = build_trajectory_observation(
        steps=steps, terminal={"status": "SUCCEEDED"}
    )

    metrics = evaluate_trajectory({}, observation)
    assert metrics["recovery_rate"].value == 1


def test_safe_step_metadata_keeps_action_results() -> None:
    metadata = _safe_step_metadata(
        {
            "action_results": [
                {
                    "tool_call_id": "call-1",
                    "logical_action_id": "logical-1",
                    "tool_identity": "create_ticket",
                    "status": "SUCCEEDED",
                    "error_code": None,
                }
            ]
        }
    )

    assert metadata["action_results"][0]["logical_action_id"] == "logical-1"
    assert metadata["action_results"][0]["tool_identity"] == "create_ticket"
