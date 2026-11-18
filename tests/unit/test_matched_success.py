from uuid import uuid4

from packages.evaluation.release_gate import EvaluationReleaseGateService
from packages.evaluation.statistics_service import _matched_success


def test_only_shared_repetitions_contribute_and_each_case_has_equal_weight():
    baseline, candidate = uuid4(), uuid4()
    trials = {
        "improved": {0: {baseline: 0, candidate: 1}, 1: {baseline: 0, candidate: 1}},
        "regressed": {0: {baseline: 1, candidate: 0}, 1: {baseline: 0, candidate: None}},
        "disjoint": {0: {baseline: 1, candidate: None}, 1: {baseline: None, candidate: 0}},
    }
    report = _matched_success(trials, baseline, candidate)
    assert report == _matched_success(dict(reversed(trials.items())), baseline, candidate)
    assert report["pair_count"] == 2
    assert report["delta"] == 0
    assert report["planned_repetitions"] == 6 and report["missing_repetitions"] == 3
    disjoint, improved, regressed = report["cases"]
    assert disjoint["matched_repetitions"] == 0 and disjoint["delta"] is None
    assert disjoint["baseline_missing"] == disjoint["candidate_missing"] == 1
    assert improved["delta"] == 1 and regressed["delta"] == -1


def test_missing_repetitions_block_a_passing_interval():
    result = EvaluationReleaseGateService()._evaluate_paired_interval(
        {"metric": "success_rate_delta", "rule": "MIN_CI_LOWER_BOUND", "threshold": 0},
        {
            "paired": {
                "interval": {"lower": 0.1, "upper": 0.5},
                "delta": 0.3,
                "pair_count": 20,
                "planned_repetitions": 40,
                "missing_repetitions": 1,
            }
        },
    )
    assert result["status"] == "INCONCLUSIVE"
    assert result["reason"] == "incomplete_paired_repetitions"
