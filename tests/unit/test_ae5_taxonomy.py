"""AE-5 rule-based failure taxonomy contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from packages.control_plane.rbac import EVALUATION_READ
from packages.core.config.settings import Settings
from packages.evaluation.taxonomy import classify_failure, summarize_labels
from packages.evaluation.trajectory import build_trajectory_observation


def _observation(*, steps=None, approvals=(), terminal=None, expected=None):
    return build_trajectory_observation(
        steps=steps or [],
        approvals=approvals,
        terminal=terminal or {"status": "SUCCEEDED", "failure_code": None},
        expected=expected,
        category="TOOL",
    )


def _tool(identity: str, hash_value: str = "h") -> dict:
    return {
        "index": 0,
        "kind": "TOOL_PROPOSAL",
        "status": "SUCCEEDED",
        "tool_identities": [identity],
        "arguments_hashes": [hash_value],
    }


def test_forbidden_tool_is_labeled() -> None:
    observation = _observation(steps=[_tool("delete_account")])

    labels = classify_failure({"forbidden_tools": ["delete_account"]}, observation)

    assert "FORBIDDEN_TOOL_CALL" in {label["code"] for label in labels}


def test_missing_required_tool_is_labeled() -> None:
    observation = _observation(steps=[_tool("query_customer")])

    labels = classify_failure(
        {"required_tools": ["query_customer", "get_order"]}, observation
    )

    codes = {label["code"] for label in labels}
    assert "REQUIRED_TOOL_MISSING" in codes
    missing = next(label for label in labels if label["code"] == "REQUIRED_TOOL_MISSING")
    assert missing["detail"]["missing"] == ["get_order"]


def test_no_tool_calls_is_labeled() -> None:
    observation = _observation(steps=[])

    labels = classify_failure({"required_tools": ["query_customer"]}, observation)

    codes = {label["code"] for label in labels}
    assert "NO_TOOL_CALLS" in codes
    assert "REQUIRED_TOOL_MISSING" in codes


def test_order_violation_is_labeled() -> None:
    steps = [_tool("create_ticket"), _tool("query_customer")]
    observation = _observation(steps=steps)

    labels = classify_failure(
        {"precedence": [["query_customer", "create_ticket"]]}, observation
    )

    assert "TOOL_ORDER_VIOLATION" in {label["code"] for label in labels}


@pytest.mark.parametrize("later_lookup", [False, True])
def test_order_violation_at_step_zero_survives_later_error(later_lookup: bool) -> None:
    steps = [_tool("create_ticket")]
    if later_lookup:
        steps.append({**_tool("query_customer"), "index": 1})
    steps.append({"index": 7, "kind": "GUARD", "status": "FAILED"})
    labels = classify_failure(
        {"precedence": [["query_customer", "create_ticket"]]}, _observation(steps=steps)
    )
    violation = next(label for label in labels if label["code"] == "TOOL_ORDER_VIOLATION")
    assert violation["step_index"] == 0


@pytest.mark.asyncio
async def test_taxonomy_route_reports_incompatible_historical_evaluator() -> None:
    workspace_id, run_id = uuid4(), uuid4()
    session = AsyncMock()
    session.scalar.side_effect = [
        SimpleNamespace(status="SUCCEEDED", experiment_id=uuid4()),
        SimpleNamespace(spec_json={"frozen": True}, evaluator_manifest={"evaluator_versions": {}}),
    ]
    app = create_app(Settings(testing=True))
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_workspace_context] = lambda: SimpleNamespace(
        workspace_id=str(workspace_id), permissions={EVALUATION_READ}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/workspaces/{workspace_id}/evaluation/experiment-runs/{run_id}/failure-taxonomy"
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPERIMENT_EVALUATOR_VERSION_MISMATCH"


def test_approval_bypass_is_labeled() -> None:
    observation = _observation(
        steps=[_tool("create_ticket")],
        approvals=[
            {
                "approval_id": "a",
                "decision_status": "DENIED",
                "execution_status": "SUCCEEDED",
            }
        ],
    )

    labels = classify_failure({"decision": "DENIED"}, observation)

    assert "APPROVAL_BYPASS" in {label["code"] for label in labels}


def test_loop_and_redundancy_are_labeled() -> None:
    steps = [
        _tool("get_order", "same"),
        _tool("get_order", "same"),
        {
            "index": 9,
            "kind": "GUARD",
            "status": "FAILED",
            "error_code": "AGENT_IDENTICAL_TOOL_CALL_LIMIT",
        },
    ]
    observation = _observation(steps=steps)

    codes = {label["code"] for label in classify_failure({}, observation)}

    assert "AGENT_LOOP" in codes
    assert "EXCESSIVE_TOOL_CALLS" in codes


def test_failed_status_is_labeled() -> None:
    observation = _observation(
        steps=[_tool("get_order")],
        terminal={"status": "FAILED", "failure_code": "TOOL_ERROR"},
    )

    labels = classify_failure({}, observation)

    assert "RUN_FAILED" in {label["code"] for label in labels}


def test_answer_mismatch_is_last_resort() -> None:
    observation = _observation(steps=[_tool("get_order")])

    labels = classify_failure({}, observation, task_success=False)

    assert labels and labels[0]["code"] == "ANSWER_MISMATCH"


def test_no_trajectory_means_no_labels() -> None:
    assert classify_failure({"required_tools": ["x"]}, {"category": "TOOL"}) == []


def test_summarize_labels_counts_codes() -> None:
    labels = [{"code": "A"}, {"code": "A"}, {"code": "B"}]

    assert summarize_labels(labels) == {"A": 2, "B": 1}
