"""AE-3 evidence-level contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.release_gate import EvaluationReleaseGateService


def _policy(level: str) -> dict:
    service = EvaluationReleaseGateService()
    return service._validate_policy(
        {
            "rules": [
                {
                    "metric": "evidence_level",
                    "rule": "REQUIRED_EVIDENCE_LEVEL",
                    "level": level,
                }
            ]
        }
    )


def _comparison() -> SimpleNamespace:
    return SimpleNamespace(status="COMPLETE", metrics={})


def test_evidence_level_policy_is_validated() -> None:
    policy = _policy("REAL_PROVIDER")

    assert policy["rules"][0]["rule"] == "REQUIRED_EVIDENCE_LEVEL"
    assert policy["rules"][0]["level"] == "REAL_PROVIDER"


def test_unknown_evidence_level_is_rejected() -> None:
    with pytest.raises(AgentHubError):
        _policy("PRODUCTION")


def test_real_provider_satisfies_real_provider_gate() -> None:
    service = EvaluationReleaseGateService()
    run = SimpleNamespace(evidence_level="REAL_PROVIDER")

    status, results, _ = service._evaluate(
        _comparison(), _policy("REAL_PROVIDER"), run
    )

    assert status == "PASS"
    assert results[0]["actual_level"] == "REAL_PROVIDER"


def test_conformance_cannot_satisfy_real_provider_gate() -> None:
    service = EvaluationReleaseGateService()
    run = SimpleNamespace(evidence_level="DETERMINISTIC_CONFORMANCE")

    status, results, reasons = service._evaluate(
        _comparison(), _policy("REAL_PROVIDER"), run
    )

    assert status == "FAIL"
    assert results[0]["status"] == "FAIL"
    assert reasons == ["evidence_level:FAIL"]


def test_missing_run_makes_evidence_gate_inconclusive() -> None:
    service = EvaluationReleaseGateService()

    status, results, _ = service._evaluate(_comparison(), _policy("REAL_PROVIDER"))

    assert status == "INCONCLUSIVE"
    assert results[0]["reason"] == "evidence_level_unknown"


def test_create_experiment_rejects_unknown_level() -> None:
    with pytest.raises(AgentHubError):
        ExperimentService._validate_evidence_level("NOT_A_LEVEL")


def test_create_experiment_accepts_known_levels() -> None:
    for level in (
        "DETERMINISTIC_CONFORMANCE",
        "CONTROLLED_SIMULATION",
        "REAL_PROVIDER",
        "PRODUCTION_SAMPLE",
    ):
        ExperimentService._validate_evidence_level(level)


def _pass_hat_policy(threshold: float) -> dict:
    return EvaluationReleaseGateService()._validate_policy(
        {
            "rules": [
                {
                    "metric": "pass_hat_k",
                    "rule": "MIN_PASS_HAT_K",
                    "threshold": threshold,
                }
            ]
        }
    )


def _comparison() -> SimpleNamespace:
    return SimpleNamespace(
        status="COMPLETE", metrics={}, candidate_variant_id="candidate"
    )


def test_pass_hat_k_gate_passes_when_value_meets_threshold() -> None:
    service = EvaluationReleaseGateService()
    statistics = {
        "variants": {"candidate": {"pass_hat_k": 0.9, "complete_case_rate": 1.0}}
    }

    status, results, _ = service._evaluate(
        _comparison(), _pass_hat_policy(0.8), None, statistics
    )

    assert status == "PASS"
    assert results[0]["candidate"] == 0.9


def test_pass_hat_k_gate_fails_below_threshold() -> None:
    service = EvaluationReleaseGateService()
    statistics = {
        "variants": {"candidate": {"pass_hat_k": 0.5, "complete_case_rate": 1.0}}
    }

    status, _, reasons = service._evaluate(
        _comparison(), _pass_hat_policy(0.8), None, statistics
    )

    assert status == "FAIL"
    assert reasons == ["pass_hat_k:FAIL"]


def test_pass_hat_k_gate_is_inconclusive_when_many_cases_are_incomplete() -> None:
    service = EvaluationReleaseGateService()
    statistics = {
        "variants": {"candidate": {"pass_hat_k": 0.9, "complete_case_rate": 0.2}}
    }

    status, results, _ = service._evaluate(
        _comparison(), _pass_hat_policy(0.8), None, statistics
    )

    assert status == "INCONCLUSIVE"
    assert results[0]["reason"] == "insufficient_complete_cases"


def test_pass_hat_k_gate_without_statistics_is_inconclusive() -> None:
    service = EvaluationReleaseGateService()

    status, results, _ = service._evaluate(
        _comparison(), _pass_hat_policy(0.8), None, None
    )

    assert status == "INCONCLUSIVE"
    assert results[0]["reason"] == "statistics_unavailable"


def _paired_policy(rule: str, threshold: float) -> dict:
    return EvaluationReleaseGateService()._validate_policy(
        {
            "rules": [
                {
                    "metric": "success_rate_delta",
                    "rule": rule,
                    "threshold": threshold,
                }
            ]
        }
    )


def _paired_statistics(lower: float) -> dict:
    return {
        "paired": {
            "delta": 0.06,
            "interval": {"lower": lower, "upper": 0.11},
            "pair_count": 100,
        }
    }


def test_min_ci_lower_bound_gate() -> None:
    service = EvaluationReleaseGateService()
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={},
        baseline_variant_id="baseline",
        candidate_variant_id="candidate",
    )

    pass_status, _, _ = service._evaluate(
        comparison, _paired_policy("MIN_CI_LOWER_BOUND", 0.02), None, _paired_statistics(0.05)
    )
    fail_status, _, _ = service._evaluate(
        comparison, _paired_policy("MIN_CI_LOWER_BOUND", 0.08), None, _paired_statistics(0.05)
    )

    assert pass_status == "PASS"
    assert fail_status == "FAIL"


def test_non_inferiority_gate_uses_regression_margin() -> None:
    service = EvaluationReleaseGateService()
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={},
        baseline_variant_id="baseline",
        candidate_variant_id="candidate",
    )

    pass_status, _, _ = service._evaluate(
        comparison,
        _paired_policy("NON_INFERIORITY", 0.05),
        None,
        _paired_statistics(-0.01),
    )
    fail_status, _, _ = service._evaluate(
        comparison,
        _paired_policy("NON_INFERIORITY", 0.05),
        None,
        _paired_statistics(-0.10),
    )

    assert pass_status == "PASS"
    assert fail_status == "FAIL"


def test_paired_gate_is_inconclusive_with_too_few_pairs() -> None:
    service = EvaluationReleaseGateService()
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={},
        baseline_variant_id="baseline",
        candidate_variant_id="candidate",
    )
    statistics = {
        "paired": {
            "delta": 0.06,
            "interval": {"lower": 0.05, "upper": 0.11},
            "pair_count": 3,
        }
    }

    status, results, _ = service._evaluate(
        comparison, _paired_policy("MIN_CI_LOWER_BOUND", 0.02), None, statistics
    )

    assert status == "INCONCLUSIVE"
    assert results[0]["reason"] == "insufficient_pairs"


def test_paired_gate_is_inconclusive_when_interval_is_degenerate() -> None:
    service = EvaluationReleaseGateService()
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={},
        baseline_variant_id="baseline",
        candidate_variant_id="candidate",
    )
    statistics = {
        "paired": {
            "delta": 0.0,
            "interval": {"lower": 0.0, "upper": 0.0},
            "pair_count": 100,
        }
    }

    status, results, _ = service._evaluate(
        comparison, _paired_policy("MIN_CI_LOWER_BOUND", 0.02), None, statistics
    )

    assert status == "INCONCLUSIVE"
    assert results[0]["reason"] == "degenerate_interval"
