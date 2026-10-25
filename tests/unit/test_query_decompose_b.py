"""C1b-1 unit tests: deterministic round-robin quota aggregation."""

from __future__ import annotations

import pytest

from benchmarks.public_rag.query_decompose_b import quota_top_k, round_robin_interleave


def test_interleave_takes_one_from_each_ranking_per_round() -> None:
    assert round_robin_interleave([[0, 1, 2], [10, 11, 12], [20, 21]]) == [
        0,
        10,
        20,
        1,
        11,
        21,
        2,
        12,
    ]


def test_interleave_drops_exact_duplicates_at_first_occurrence() -> None:
    # doc 5 ranks high in two sub-queries: kept once at its best round.
    assert round_robin_interleave([[5, 1], [5, 2], [3, 5]]) == [5, 3, 1, 2]


def test_interleave_handles_uneven_lengths_and_empty_rankings() -> None:
    assert round_robin_interleave([[1, 2, 3], [], [7]]) == [1, 7, 2, 3]
    assert round_robin_interleave([]) == []
    assert round_robin_interleave([[], []]) == []


def test_interleave_is_deterministic_for_fixed_input() -> None:
    rankings = [[3, 1, 2], [2, 3, 0], [1, 0, 2]]
    assert round_robin_interleave(rankings) == round_robin_interleave(rankings)


def test_quota_top_k_cuts_after_interleaving() -> None:
    rankings = [[9, 8, 7], [4, 3, 2]]
    assert quota_top_k(rankings, k=3) == [9, 4, 8]
    assert quota_top_k(rankings, k=99) == [9, 4, 8, 3, 7, 2]


def test_quota_top_k_rejects_invalid_k() -> None:
    with pytest.raises(ValueError):
        quota_top_k([[1]], k=0)
    with pytest.raises(ValueError):
        quota_top_k([[1]], k=True)
