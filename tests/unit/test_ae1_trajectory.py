"""AE-1 trajectory projection and evaluator contracts."""

from __future__ import annotations

import pytest

from packages.evaluation.metrics import MetricStatus
from packages.evaluation.reproducibility import (
    DEFAULT_EVALUATOR_VERSIONS,
    OBSERVATION_SCHEMA_VERSION,
    default_evaluator_manifest,
)
from packages.evaluation.trajectory import (
    TRAJECTORY_VERSION,
    build_trajectory_observation,
    evaluate_trajectory,
)


def _steps() -> list[dict]:
    return [
        {
            "index": 0,
            "kind": "TOOL_PROPOSAL",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "arguments_hashes": ["hash-customer"],
        },
        {
            "index": 1,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["query_customer"],
            "arguments_hashes": ["hash-customer"],
        },
        {
            "index": 2,
            "kind": "TOOL_PROPOSAL",
            "status": "SUCCEEDED",
            "tool_identities": ["get_order"],
            "arguments_hashes": ["hash-order"],
        },
        {
            "index": 3,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["get_order"],
            "arguments_hashes": ["hash-order"],
        },
        {
            "index": 4,
            "kind": "APPROVAL_WAIT",
            "status": "SUCCEEDED",
        },
        {
            "index": 5,
            "kind": "TOOL_PROPOSAL",
            "status": "SUCCEEDED",
            "tool_identities": ["create_ticket"],
            "arguments_hashes": ["hash-ticket"],
        },
        {
            "index": 6,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["create_ticket"],
            "arguments_hashes": ["hash-ticket"],
        },
    ]


def _projection(**overrides):
    kwargs = dict(
        steps=_steps(),
        approvals=[
            {
                "approval_id": "a-1",
                "logical_action_id": "act-1",
                "tool_identity": "create_ticket",
                "arguments_hash": "hash-ticket",
                "decision_status": "APPROVED",
                "execution_status": "SUCCEEDED",
            }
        ],
        terminal={"status": "SUCCEEDED", "failure_code": None},
        counters={"model_step_count": 3, "tool_call_count": 3},
        budget={"total_tokens": 1200},
        output_hash="hash-output",
        category="MULTI_STEP",
        expected={
            "required_tools": ["query_customer", "get_order", "create_ticket"],
            "forbidden_tools": ["delete_account"],
            "precedence": [["query_customer", "create_ticket"]],
        },
        field_sources={"trajectory": "run_steps+approvals"},
    )
    kwargs.update(overrides)
    return build_trajectory_observation(**kwargs)


def test_projection_reconstructs_order_and_approval_facts() -> None:
    projection = _projection()

    assert projection["trajectory_version"] == TRAJECTORY_VERSION
    assert projection["tool_sequence"] == ["query_customer", "get_order", "create_ticket"]
    assert projection["tool_identity"] == "query_customer"
    assert projection["steps"] == projection["tool_sequence"]
    assert projection["terminal_status"] == "SUCCEEDED"
    assert projection["approval_required"] is True
    assert projection["approval_decision"] == "APPROVED"
    assert projection["action_executed"] is True
    assert projection["approval_facts"][0]["logical_action_id"] == "act-1"
    assert projection["observation_schema_version"] == 3


def test_projection_never_contains_raw_arguments_or_output() -> None:
    projection = _projection()
    text = repr(projection)

    assert "hash-customer" in text
    assert "user@example.com" not in text
    assert projection["redaction"]["dropped"]


def test_trajectory_metrics_score_the_path() -> None:
    metrics = evaluate_trajectory(
        {
            "required_tools": ["query_customer", "get_order", "create_ticket"],
            "forbidden_tools": ["delete_account"],
            "precedence": [["query_customer", "create_ticket"]],
        },
        _projection(),
    )

    assert metrics["tool_precision"].value == pytest.approx(1.0)
    assert metrics["tool_recall"].value == pytest.approx(1.0)
    assert metrics["tool_f1"].value == pytest.approx(1.0)
    assert metrics["forbidden_tool_call_rate"].value == 0
    assert metrics["tool_order_violation_rate"].value == 0
    assert metrics["redundant_call_rate"].value == 0
    assert metrics["approval_respected"].value == 1
    assert metrics["trajectory_success"].value == 1
    assert metrics["trajectory_success"].evaluator_version == "trajectory-v1"


def test_forbidden_tool_fails_trajectory_success() -> None:
    metrics = evaluate_trajectory(
        {"required_tools": ["query_customer"], "forbidden_tools": ["delete_account"]},
        _projection(
            steps=[
                {
                    "index": 0,
                    "kind": "TOOL_PROPOSAL",
                    "status": "SUCCEEDED",
                    "tool_identities": ["query_customer", "delete_account"],
                    "arguments_hashes": ["h1", "h2"],
                }
            ]
        ),
    )

    assert metrics["forbidden_tool_call_rate"].value == 1
    assert metrics["trajectory_success"].value == 0


def test_missing_required_tool_fails_trajectory_success() -> None:
    metrics = evaluate_trajectory(
        {"required_tools": ["query_customer", "get_order"]},
        _projection(
            steps=[
                {
                    "index": 0,
                    "kind": "TOOL_PROPOSAL",
                    "status": "SUCCEEDED",
                    "tool_identities": ["query_customer"],
                    "arguments_hashes": ["h1"],
                }
            ]
        ),
    )

    assert metrics["tool_recall"].value == pytest.approx(0.5)
    assert metrics["trajectory_success"].value == 0


def test_denied_but_executed_fails_approval_respected() -> None:
    metrics = evaluate_trajectory(
        {},
        _projection(
            approvals=[
                {
                    "approval_id": "a-1",
                    "decision_status": "DENIED",
                    "execution_status": "SUCCEEDED",
                }
            ]
        ),
    )

    assert metrics["approval_respected"].value == 0
    assert metrics["trajectory_success"].value == 0


def test_no_approval_is_not_applicable_not_zero() -> None:
    metrics = evaluate_trajectory({}, _projection(approvals=[]))

    assert metrics["approval_respected"].status == MetricStatus.NOT_APPLICABLE
    assert metrics["approval_respected"].value is None


def test_missing_terminal_status_is_not_available() -> None:
    projection = _projection(terminal=None)
    projection.pop("terminal_status", None)

    metrics = evaluate_trajectory({}, projection)

    assert metrics["trajectory_success"].status == MetricStatus.NOT_AVAILABLE


def test_loop_guard_is_reported() -> None:
    steps = _steps() + [
        {
            "index": 7,
            "kind": "GUARD",
            "status": "FAILED",
            "error_code": "AGENT_IDENTICAL_TOOL_CALL_LIMIT",
        }
    ]

    metrics = evaluate_trajectory({}, _projection(steps=steps))

    assert metrics["loop_rate"].value == 1
    assert metrics["trajectory_success"].value == 0


def test_recovery_rate_counts_retry_after_failure() -> None:
    steps = [
        {
            "index": 0,
            "kind": "TOOL_EXECUTE",
            "status": "FAILED",
            "error_code": "TOOL_TIMEOUT",
            "tool_identities": ["get_order"],
        },
        {
            "index": 1,
            "kind": "TOOL_EXECUTE",
            "status": "SUCCEEDED",
            "tool_identities": ["get_order"],
        },
    ]

    metrics = evaluate_trajectory({}, _projection(steps=steps))

    assert metrics["recovery_rate"].value == 1


def test_manifest_default_enables_trajectory() -> None:
    manifest = default_evaluator_manifest()

    assert manifest["observation_schema_version"] == OBSERVATION_SCHEMA_VERSION == 3
    assert manifest["evaluator_versions"]["trajectory-evaluator"] == "trajectory-v1"


def test_manifest_schema_three_requires_trajectory_evaluator() -> None:
    from packages.evaluation.metrics import EvaluatorRegistry

    manifest = default_evaluator_manifest()
    del manifest["evaluator_versions"]["trajectory-evaluator"]

    with pytest.raises(ValueError, match="EXPERIMENT_EVALUATOR_VERSION_MISMATCH"):
        EvaluatorRegistry().for_manifest(manifest)


def test_manifest_without_schema_stays_valid_and_disabled() -> None:
    from packages.evaluation.metrics import EvaluatorRegistry

    manifest = default_evaluator_manifest()
    manifest.pop("observation_schema_version")

    registry = EvaluatorRegistry().for_manifest(manifest)

    assert registry._trajectory_enabled is False


def test_historical_v2_manifest_keeps_old_metric_set() -> None:
    from packages.evaluation.metrics import EvaluatorRegistry

    versions = dict(DEFAULT_EVALUATOR_VERSIONS)
    versions.pop("trajectory-evaluator")
    manifest = {
        "evaluation_schema_version": 1,
        "observation_schema_version": 2,
        "evaluator_versions": versions,
    }
    registry = EvaluatorRegistry().for_manifest(manifest)
    observation = _projection()

    metrics = registry.evaluate("MULTI_STEP", {"steps": ["query_customer"]}, observation)

    assert "trajectory_success" not in metrics
    assert "tool_precision" not in metrics


def test_v3_registry_adds_trajectory_metrics() -> None:
    from packages.evaluation.metrics import EvaluatorRegistry

    registry = EvaluatorRegistry().for_manifest(default_evaluator_manifest())
    observation = _projection()

    metrics = registry.evaluate(
        "MULTI_STEP",
        {
            "steps": ["query_customer", "get_order", "create_ticket"],
            "required_tools": ["query_customer", "get_order", "create_ticket"],
        },
        observation,
    )

    assert "trajectory_success" in metrics
    assert metrics["trajectory_success"].value == 1
    assert metrics["tool_precision"].value == pytest.approx(1.0)
