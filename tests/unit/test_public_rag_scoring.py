from __future__ import annotations

import math

import pytest

from benchmarks.public_rag.scoring import (
    METRIC_NAMES,
    evaluate_questions,
    paired_bootstrap_comparison,
)


@pytest.fixture
def graded_case() -> tuple[dict[str, dict[str, int]], dict[str, tuple[str, ...]]]:
    return (
        {"q1": {"doc-a": 2, "doc-b": 1}},
        {"q1": ("doc-a", "doc-a", "doc-noise", "doc-b")},
    )


def test_graded_document_metrics_match_hand_calculation(
    graded_case: tuple[dict[str, dict[str, int]], dict[str, tuple[str, ...]]],
) -> None:
    qrels, rankings = graded_case
    evaluation = evaluate_questions(qrels, rankings)
    question = evaluation.questions["q1"]

    dcg_at_5 = 3.0 / math.log2(2) + 1.0 / math.log2(4)
    ideal_dcg_at_5 = 3.0 / math.log2(2) + 1.0 / math.log2(3)

    assert question.ranking == ("doc-a", "doc-noise", "doc-b")
    assert question.grades == (2, 0, 1)
    assert question.missing_documents == ()
    assert question.ranking_present
    assert question.gold_coverage == 1.0
    assert not question.skipped
    assert question.metrics["Recall@5"] == 1.0
    assert question.metrics["Recall@10"] == 1.0
    assert question.metrics["Recall@20"] == 1.0
    assert question.metrics["MRR@5"] == 1.0
    assert question.metrics["MRR@10"] == 1.0
    assert question.metrics["nDCG@5"] == pytest.approx(dcg_at_5 / ideal_dcg_at_5)
    assert question.metrics["nDCG@10"] == pytest.approx(dcg_at_5 / ideal_dcg_at_5)
    assert question.metrics["nDCG@20"] == pytest.approx(dcg_at_5 / ideal_dcg_at_5)

    assert evaluation.evaluated_count == 1
    assert evaluation.skipped_unjudged_count == 0
    assert evaluation.skipped_no_positive_count == 0
    assert evaluation.skipped_count == 0
    assert evaluation.evaluation_coverage == 1.0
    assert evaluation.missing_ranking_count == 0
    assert evaluation.missing_document_count == 0
    assert evaluation.gold_coverage == 1.0
    for metric in METRIC_NAMES:
        assert evaluation.metrics[metric] == pytest.approx(question.metrics[metric])


def test_empty_ranking_is_evaluated_as_zero_and_reports_missing_gold() -> None:
    evaluation = evaluate_questions(
        {"q1": {"doc-a": 2, "doc-b": 1}},
        {},
    )
    question = evaluation.questions["q1"]

    assert question.evaluated
    assert not question.ranking_present
    assert question.gold_coverage == 0.0
    assert question.ranking == ()
    assert question.missing_documents == ("doc-a", "doc-b")
    assert all(question.metrics[metric] == 0.0 for metric in METRIC_NAMES)
    assert evaluation.evaluated_count == 1
    assert evaluation.evaluation_coverage == 1.0
    assert evaluation.missing_ranking_count == 1
    assert evaluation.missing_document_count == 2
    assert evaluation.gold_coverage == 0.0


def test_unjudged_and_no_positive_questions_are_reported_as_skipped() -> None:
    evaluation = evaluate_questions(
        {"no-positive": {"doc-a": 0}},
        {
            "no-positive": ("doc-a",),
            "unjudged": ("doc-a", "doc-a"),
        },
    )

    assert evaluation.question_count == 2
    assert evaluation.evaluated_count == 0
    assert evaluation.skipped_count == 2
    assert evaluation.evaluation_coverage == 0.0
    assert evaluation.missing_ranking_count == 0
    assert evaluation.skipped_no_positive_count == 1
    assert evaluation.skipped_unjudged_count == 1
    assert not evaluation.questions["no-positive"].evaluated
    assert evaluation.questions["no-positive"].ranking_present
    assert evaluation.questions["no-positive"].skipped_no_positive
    assert not evaluation.questions["unjudged"].evaluated
    assert evaluation.questions["unjudged"].ranking_present
    assert evaluation.questions["unjudged"].skipped_unjudged
    assert evaluation.questions["unjudged"].ranking == ("doc-a",)
    assert evaluation.metrics == {metric: 0.0 for metric in METRIC_NAMES}


def test_paired_bootstrap_is_reproducible_and_uses_gold_overlap_groups() -> None:
    qrels = {
        "q1": {"doc-a": 1, "doc-b": 1},
        "q2": {"doc-b": 1},
        "q3": {"doc-c": 1},
        "q4": {"doc-d": 1},
    }
    baseline = evaluate_questions(
        qrels,
        {
            "q1": (),
            "q2": (),
            "q3": (),
            "q4": (),
        },
    )
    candidate = evaluate_questions(
        qrels,
        {
            "q1": ("doc-a",),
            "q2": ("doc-b",),
            "q3": ("doc-c",),
            "q4": (),
        },
    )

    first = paired_bootstrap_comparison(
        baseline,
        candidate,
        metric="Recall@5",
        sample_count=200,
        confidence=0.95,
        seed=17,
    )
    repeated = paired_bootstrap_comparison(
        baseline,
        candidate,
        metric="Recall@5",
        sample_count=200,
        confidence=0.95,
        seed=17,
    )

    assert first.estimate == pytest.approx(0.625)
    assert first.group_count == 3
    assert first.sample_count == 200
    assert first.confidence == 0.95
    assert first.ci_low <= first.estimate <= first.ci_high
    assert first == repeated


def test_duplicate_chunks_are_deduplicated_before_cutoff_ranks() -> None:
    ranking = (
        "noise-1",
        "noise-1",
        "noise-2",
        "noise-2",
        "noise-3",
        "noise-3",
        "noise-4",
        "noise-4",
        "noise-5",
        "noise-5",
        "doc-a",
        "noise-6",
        "noise-6",
        "noise-7",
        "noise-7",
        "noise-8",
        "noise-8",
        "noise-9",
        "noise-9",
        "doc-b",
        "noise-10",
        "noise-10",
        "noise-11",
        "noise-11",
        "noise-12",
        "noise-12",
        "noise-13",
        "noise-13",
        "noise-14",
        "noise-14",
        "noise-15",
        "noise-15",
        "noise-16",
        "noise-16",
        "noise-17",
        "noise-17",
        "noise-18",
        "noise-18",
        "doc-c",
    )
    evaluation = evaluate_questions(
        {"q1": {"doc-a": 2, "doc-b": 1, "doc-c": 3}},
        {"q1": ranking},
    )
    question = evaluation.questions["q1"]

    assert len(ranking) == 39
    assert len(question.ranking) == 21
    assert question.ranking.index("doc-a") + 1 == 6
    assert question.ranking.index("doc-b") + 1 == 11
    assert question.ranking.index("doc-c") + 1 == 21

    # Positive grades are 2, 1, 3 at deduplicated ranks 6, 11, 21.
    dcg_at_10 = 3.0 / math.log2(7)
    dcg_at_20 = 3.0 / math.log2(7) + 1.0 / math.log2(12)
    ideal_dcg_at_20 = 7.0 / math.log2(2) + 3.0 / math.log2(3) + 1.0 / math.log2(4)

    assert question.metrics["Recall@5"] == 0.0
    assert question.metrics["Recall@10"] == pytest.approx(1.0 / 3.0)
    assert question.metrics["Recall@20"] == pytest.approx(2.0 / 3.0)
    assert question.metrics["MRR@5"] == 0.0
    assert question.metrics["MRR@10"] == pytest.approx(1.0 / 6.0)
    assert question.metrics["nDCG@5"] == 0.0
    assert question.metrics["nDCG@10"] == pytest.approx(dcg_at_10 / ideal_dcg_at_20)
    assert question.metrics["nDCG@20"] == pytest.approx(dcg_at_20 / ideal_dcg_at_20)


def test_bootstrap_groups_transitive_gold_overlap_and_rejects_mismatch() -> None:
    qrels = {
        "q1": {"doc-a": 1},
        "q2": {"doc-a": 1, "doc-b": 1},
        "q3": {"doc-b": 1},
        "q4": {"doc-c": 1},
    }
    baseline = evaluate_questions(
        qrels,
        {
            "q1": (),
            "q2": (),
            "q3": (),
            "q4": (),
        },
    )
    candidate = evaluate_questions(
        qrels,
        {
            "q1": ("doc-a",),
            "q2": ("doc-a",),
            "q3": ("doc-b",),
            "q4": ("doc-c",),
        },
    )

    comparison = paired_bootstrap_comparison(
        baseline,
        candidate,
        metric="Recall@5",
        sample_count=100,
        seed=7,
    )

    # q1-q2-q3 form one component through {a,b}; q4 is the second component.
    assert comparison.group_count == 2

    mismatched = evaluate_questions(
        {
            "q1": {"doc-a": 1},
            "q2": {"doc-a": 1, "doc-b": 1},
            "q3": {"doc-b": 1},
            "q4": {"doc-d": 1},
        },
        {
            "q1": ("doc-a",),
            "q2": ("doc-a",),
            "q3": ("doc-b",),
            "q4": ("doc-d",),
        },
    )
    with pytest.raises(ValueError, match="gold documents differ for q4"):
        paired_bootstrap_comparison(
            baseline,
            mismatched,
            metric="Recall@5",
            sample_count=100,
            seed=7,
        )
