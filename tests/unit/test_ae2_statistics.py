"""AE-2 statistics contracts."""

from __future__ import annotations

import pytest

from packages.evaluation.statistics import (
    TrialOutcome,
    cluster_bootstrap_interval,
    holm_adjust,
    jeffreys_interval,
    mcnemar_exact,
    minimum_detectable_effect,
    paired_bootstrap_interval,
    required_pairs,
    summarize_reliability,
)


def _outcomes() -> list[TrialOutcome]:
    return [
        TrialOutcome("a", 0, True, "h-a"),
        TrialOutcome("a", 1, True, "h-a"),
        TrialOutcome("a", 2, True, "h-a"),
        TrialOutcome("b", 0, True, "h-b1"),
        TrialOutcome("b", 1, True, "h-b2"),
        TrialOutcome("b", 2, False, "h-b3"),
        TrialOutcome("c", 0, True, "h-c1"),
        TrialOutcome("c", 1, False, "h-c2"),
        TrialOutcome("c", 2, False, "h-c3"),
        TrialOutcome("d", 0, True, "h-d"),
        TrialOutcome("d", 1, None, None),
    ]


def test_pass_hat_k_and_variance() -> None:
    summary = summarize_reliability(_outcomes(), k=3)

    assert summary["case_count"] == 4
    assert summary["complete_case_count"] == 3
    assert summary["incomplete_case_count"] == 1
    assert summary["success_count"] == 7
    assert summary["known_trial_count"] == 10
    assert summary["success_rate"] == pytest.approx(0.7)
    assert summary["pass_hat_k"] == pytest.approx(1 / 3)
    assert summary["pass_hat_k_all_cases"] == pytest.approx(1 / 4)
    assert summary["complete_case_rate"] == pytest.approx(0.75)
    assert summary["success_coverage_rate"] == pytest.approx(10 / 11)
    assert summary["case_all_success"] == {"a": True, "b": False, "c": False}
    assert summary["trial_variance"] is not None


def test_missing_trial_never_counts_as_success() -> None:
    summary = summarize_reliability(
        [
            TrialOutcome("a", 0, True, "h"),
            TrialOutcome("a", 1, None, None),
        ],
        k=2,
    )

    assert summary["complete_case_count"] == 0
    assert summary["pass_hat_k"] is None
    assert summary["incomplete_case_count"] == 1


def test_consistency_requires_same_output_hash() -> None:
    summary = summarize_reliability(_outcomes(), k=3)

    # Only case "a" has all successes and one hash; b/c differ or fail.
    assert summary["consistency_at_k"] == pytest.approx(1 / 3)


def test_jeffreys_interval_matches_known_values() -> None:
    lower, upper = jeffreys_interval(5, 10)

    assert lower == pytest.approx(0.22352867025270517, abs=1e-9)
    assert upper == pytest.approx(0.7764713297472947, abs=1e-9)


def test_bootstrap_is_reproducible_and_brackets_the_point() -> None:
    values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0]
    first = cluster_bootstrap_interval(values, seed=7)
    second = cluster_bootstrap_interval(values, seed=7)

    assert first == second
    assert first is not None
    assert first[0] <= 0.5 <= first[1]


def test_paired_bootstrap_uses_differences() -> None:
    pairs = [(0.0, 1.0), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    interval = paired_bootstrap_interval(pairs, seed=3)

    assert interval is not None
    assert interval[0] <= 0.5 <= interval[1]


def test_mcnemar_exact_matches_hand_calculation() -> None:
    # 2 * (C(12,0) + C(12,1) + C(12,2)) / 2**12 = 158 / 4096
    assert mcnemar_exact(10, 2) == pytest.approx(158 / 4096)


def test_holm_adjustment() -> None:
    adjusted = holm_adjust([("a", 0.01), ("b", 0.04), ("c", 0.03)])

    assert adjusted["a"] == pytest.approx(0.03)
    assert adjusted["b"] == pytest.approx(0.06)
    assert adjusted["c"] == pytest.approx(0.06)


def test_required_pairs_and_mde_are_consistent() -> None:
    needed = required_pairs(0.05, 0.2)

    assert needed == 626
    assert minimum_detectable_effect(needed, 0.2) == pytest.approx(0.05, abs=1e-3)


def test_mde_returns_none_when_underpowered() -> None:
    assert minimum_detectable_effect(5, 0.2) is None
