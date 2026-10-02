from dataclasses import replace

import pytest

from packages.evaluation.quality_audit import (
    ClaimReview,
    JudgeReview,
    TrialObservation,
    claim_metrics,
    judge_metrics,
    trial_metrics,
)


def test_three_trials_keep_missing_tasks_and_variants_in_plan():
    observations = [TrialObservation("a", "base", i, True, False, 10) for i in range(3)]
    observations += [TrialObservation("b", "base", 0, True, True, 20)]
    metrics = trial_metrics(["a", "b"], ["base", "candidate"], observations)
    base = metrics["base"]["all_3_trials_success_rate"]
    assert base["value"] == 0.5 and base["denominator"] == 2
    assert base["details"]["incomplete_task_count"] == 1
    assert metrics["base"]["business_trial_success_rate"]["details"]["unknown_count"] == 2
    assert metrics["candidate"]["all_3_trials_success_rate"]["status"] == "NOT_AVAILABLE"


def test_duplicates_unplanned_trials_and_invalid_plan_are_rejected():
    row = TrialObservation("a", "base", 0)
    for rows, message in (([row, row], "DUPLICATE"), ([replace(row, case_id="b")], "UNPLANNED")):
        with pytest.raises(ValueError, match=message):
            trial_metrics(["a"], ["base"], rows)
    with pytest.raises(ValueError, match="PLAN"):
        trial_metrics(["a", "a"], ["base"], [])


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_invalid_latency_is_not_an_observation(value):
    with pytest.raises(ValueError, match="LATENCY"):
        TrialObservation("a", "base", 0, latency_ms=value)


def test_refusal_separates_answerable_and_unanswerable_denominators():
    rows = [TrialObservation("answer", "base", i, False, True, 50) for i in range(3)] + [
        TrialObservation("no-answer", "base", 0, True, True, 100)
    ]
    result = trial_metrics(
        ["answer", "no-answer", "unlabeled"],
        ["base"],
        rows,
        answer_available={"answer": True, "no-answer": False},
    )["base"]
    assert result["incorrect_refusal_rate"]["value"] == 1
    correct = result["correct_refusal_rate"]
    assert correct["denominator"] == 3 and correct["numerator"] == 1
    assert correct["details"]["unknown_count"] == 2
    assert correct["details"]["missing_answer_label_case_count"] == 1
    assert result["all_trial_latency_p50_ms"]["sample_count"] == 4
    assert result["successful_trial_latency_p50_ms"]["sample_count"] == 1


def test_claim_support_coverage_and_unverifiable_are_separate():
    result = claim_metrics(
        ["a", "b", "c", "d"],
        [
            ClaimReview("a", "human", True),
            ClaimReview("b", "human", False),
            ClaimReview("c", "human", None),
        ],
    )
    assert result["claim_support_rate"]["value"] == 0.5
    assert result["claim_review_coverage"]["value"] == 0.5
    assert result["claim_support_rate"]["details"]["unable_to_verify_count"] == 1
    assert result["claim_support_rate"]["details"]["unreviewed_count"] == 1
    assert claim_metrics([], [])["claim_support_rate"]["value"] is None
    with pytest.raises(ValueError, match="IDENTITY"):
        ClaimReview("a", "", True)


def test_judge_failures_confusion_and_small_sample_are_not_hidden():
    rows = [
        JudgeReview("tp", "SCORED", True, True, "human"),
        JudgeReview("fp", "SCORED", True, False, "human"),
        JudgeReview("parse", "UNPARSEABLE", None, True, "human"),
        JudgeReview("error", "FAILED", None, None, None),
    ]
    result = judge_metrics(rows)
    assert result["judge_agreement"]["value"] == 0.5
    assert result["judge_agreement"]["details"]["confusion_matrix"]["FP"] == 1
    assert result["judge_parse_failure_rate"]["value"] == 0.25
    assert result["judge_parse_failure_rate"]["details"]["failed_count"] == 1
    assert result["judge_kappa"]["value"] is None
    with pytest.raises(ValueError, match="DUPLICATE"):
        judge_metrics([rows[0], rows[0]])


def test_judge_kappa_requires_non_degenerate_distribution():
    balanced = [JudgeReview(str(i), "SCORED", i % 2 == 0, i % 2 == 0, "human") for i in range(20)]
    assert judge_metrics(balanced)["judge_kappa"]["value"] == 1
    uniform = [JudgeReview(str(i), "SCORED", True, True, "human") for i in range(20)]
    assert judge_metrics(uniform)["judge_kappa"]["reason"] == "degenerate_label_distribution"
