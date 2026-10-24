import math

import pytest

from benchmarks.public_rag.scenario_metrics import evidence_metrics, paired_group_ci


def test_evidence_metrics_handles_annotator_alternatives_and_duplicate_ids() -> None:
    partial = evidence_metrics([["a", "b"], ["a", "c"]], ["a", "x", "a"])
    assert partial == {
        "best_evidence_recall": 0.5,
        "complete_evidence_set": 0,
        "evidence_precision": 0.5,
        "evaluable": True,
    }

    complete = evidence_metrics([["a", "b"], ["a", "c"]], ["x", "a", "b"])
    assert complete["best_evidence_recall"] == 1.0
    assert complete["complete_evidence_set"] == 1
    assert complete["evidence_precision"] == pytest.approx(2 / 3)


def test_evidence_metrics_does_not_treat_missing_gold_or_output_as_success() -> None:
    missing_gold = evidence_metrics([[], []], ["x"])
    assert missing_gold == {
        "best_evidence_recall": 0.0,
        "complete_evidence_set": 0,
        "evidence_precision": 0.0,
        "evaluable": False,
    }

    empty_output = evidence_metrics([[], ["a"]], [])
    assert empty_output == {
        "best_evidence_recall": 0.0,
        "complete_evidence_set": 0,
        "evidence_precision": 0.0,
        "evaluable": True,
    }


def test_paired_group_ci_uses_query_weighted_constant_shift() -> None:
    rows = [
        {"query_id": "q1", "group_id": "g1", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
        {"query_id": "q2", "group_id": "g1", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
        {"query_id": "q3", "group_id": "g2", "arms": {"base": {"r": 1}, "new": {"r": 2}}},
    ]

    result = paired_group_ci(rows, "r", "base", "new", samples=20)

    assert result["baseline_mean"] == pytest.approx(1 / 3)
    assert result["candidate_mean"] == pytest.approx(4 / 3)
    assert result["difference"] == 1.0
    assert result["ci95_low"] == 1.0
    assert result["ci95_high"] == 1.0
    assert result["queries"] == 3
    assert result["groups"] == 2
    assert result["inference_valid"] is True


def test_paired_group_ci_marks_single_cluster_descriptive_only() -> None:
    rows = [
        {"query_id": "q1", "group_id": "paper", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
        {"query_id": "q2", "group_id": "paper", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
    ]

    result = paired_group_ci(rows, "r", "base", "new", samples=5)

    assert result["queries"] == 2
    assert result["groups"] == 1
    assert result["ci95_low"] == result["ci95_high"] == 1.0
    assert result["inference_valid"] is False


def test_paired_group_ci_confidence_controls_percentile_tails() -> None:
    rows = [
        {
            "query_id": f"q{index}",
            "group_id": f"g{index}",
            "arms": {"base": {"r": 0}, "new": {"r": index}},
        }
        for index in range(10)
    ]

    default = paired_group_ci(rows, "r", "base", "new", samples=500)
    family_wise = paired_group_ci(
        rows, "r", "base", "new", samples=500, confidence=0.98333
    )

    assert family_wise["ci95_low"] <= default["ci95_low"]
    assert family_wise["ci95_high"] >= default["ci95_high"]
    assert (family_wise["ci95_low"], family_wise["ci95_high"]) != (
        default["ci95_low"],
        default["ci95_high"],
    )


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {"query_id": "q1", "group_id": "g1", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
                {"query_id": "q1", "group_id": "g2", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
            ],
            "duplicate query_id",
        ),
        (
            [{"query_id": "q1", "group_id": "g1", "arms": {"base": {"r": 0}}}],
            "unpaired query",
        ),
        (
            [
                {
                    "query_id": "q1",
                    "group_id": "g1",
                    "arms": {"base": {"r": math.nan}, "new": {"r": 1}},
                }
            ],
            "finite",
        ),
    ],
)
def test_paired_group_ci_rejects_invalid_inference_inputs(rows: list[dict], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        paired_group_ci(rows, "r", "base", "new", samples=5)


@pytest.mark.parametrize("confidence", [0.0, 1.0, math.nan])
def test_paired_group_ci_rejects_invalid_confidence(confidence: float) -> None:
    rows = [
        {"query_id": "q1", "group_id": "g1", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
        {"query_id": "q2", "group_id": "g2", "arms": {"base": {"r": 0}, "new": {"r": 1}}},
    ]

    with pytest.raises(ValueError, match="between 0 and 1"):
        paired_group_ci(rows, "r", "base", "new", samples=5, confidence=confidence)
