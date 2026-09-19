from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.release_gate import EvaluationReleaseGateService


def _metric(before: float, after: float, *, direction: str = "HIGHER_IS_BETTER") -> dict:
    return {
        "status": "COMPLETE",
        "direction": direction,
        "baseline": {"status": "AVAILABLE", "value": before},
        "candidate": {"status": "AVAILABLE", "value": after},
    }


def test_release_gate_policy_validation_rejects_unknown_extra_negative_and_bad_tradeoff() -> None:
    service = EvaluationReleaseGateService()
    with pytest.raises(AgentHubError):
        service._validate_policy({"rules": [{"metric": "unknown", "rule": "NO_REGRESSION"}]})
    with pytest.raises(AgentHubError):
        service._validate_policy(
            {"rules": [{"metric": "task_success", "rule": "NO_REGRESSION", "extra": 1}]}
        )
    with pytest.raises(AgentHubError):
        service._validate_policy(
            {
                "rules": [
                    {
                        "metric": "latency_p95_ms",
                        "rule": "MAX_ABSOLUTE_REGRESSION",
                        "tolerance": -1,
                    }
                ]
            }
        )
    with pytest.raises(AgentHubError):
        service._validate_policy(
            {
                "rules": [
                    {
                        "metric": "task_success",
                        "rule": "TRADEOFF",
                        "tolerance": 1,
                        "guard_metric": "task_success",
                        "guard_rule": "NO_REGRESSION",
                    }
                ]
            }
        )


def test_release_gate_has_explicit_direction_aware_rules() -> None:
    service = EvaluationReleaseGateService()
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={
            "task_success": _metric(0.9, 0.9),
            "latency_p95_ms": _metric(100, 120, direction="LOWER_IS_BETTER"),
        },
    )
    status, results, reasons = service._evaluate(
        comparison,
        service._validate_policy(
            {
                "rules": [
                    {"metric": "task_success", "rule": "NO_REGRESSION", "safety": True},
                    {
                        "metric": "latency_p95_ms",
                        "rule": "MAX_ABSOLUTE_REGRESSION",
                        "tolerance": 25,
                    },
                ]
            }
        ),
    )

    assert status == "PASS"
    assert len(results) == 2
    assert reasons == []


def test_safety_failure_cannot_be_overridden_by_tradeoff() -> None:
    service = EvaluationReleaseGateService()
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={
            "task_success": _metric(0.9, 0.8),
            "latency_p95_ms": _metric(100, 80, direction="LOWER_IS_BETTER"),
        },
    )
    status, _results, reasons = service._evaluate(
        comparison,
        service._validate_policy(
            {
                "rules": [
                    {"metric": "task_success", "rule": "NO_REGRESSION", "safety": True},
                    {
                        "metric": "latency_p95_ms",
                        "rule": "TRADEOFF",
                        "tolerance": 100,
                        "guard_metric": "task_success",
                        "guard_rule": "NO_REGRESSION",
                    },
                ]
            }
        ),
    )

    assert status == "FAIL"
    assert "task_success:FAIL" in reasons


def test_incomplete_or_not_comparable_is_inconclusive() -> None:
    service = EvaluationReleaseGateService()
    policy = service._validate_policy(
        {"rules": [{"metric": "task_success", "rule": "NO_REGRESSION"}]}
    )
    incomplete = SimpleNamespace(status="INCOMPLETE", metrics={})
    not_comparable = SimpleNamespace(status="NOT_COMPARABLE", metrics={})

    assert service._evaluate(incomplete, policy)[0] == "INCONCLUSIVE"
    assert service._evaluate(not_comparable, policy)[0] == "INCONCLUSIVE"
