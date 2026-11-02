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
