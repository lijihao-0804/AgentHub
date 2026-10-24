from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

import pytest

from benchmarks.public_rag.project_score_fusion import distribution_scores, fuse_dbsf


def _hit(
    chunk_id: object,
    document_id: object,
    score: object,
    **extra: object,
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "score": score,
        **extra,
    }


def test_distribution_uses_sample_variance_and_degenerate_half_scores() -> None:
    assert distribution_scores((1.0, 2.0, 3.0)) == pytest.approx((1 / 3, 1 / 2, 2 / 3))
    assert distribution_scores((7.0, 7.0, 7.0)) == (0.5, 0.5, 0.5)
    assert distribution_scores((42.0,)) == (0.5,)


def test_shared_scores_sum_with_cutoff_and_final_ties_use_chunk_id() -> None:
    lanes = [
        [
            _hit("a", "doc-a", 1),
            _hit("b", "doc-b", 2),
            _hit("c", "doc-c", 3),
        ],
        [
            _hit("c", "doc-c", 30),
            _hit("a", "doc-a", 10),
            _hit("b", "doc-b", 20),
        ],
        [
            _hit("d", "doc-d", 1),
            _hit("e", "doc-e", 2),
        ],
    ]

    top_one = fuse_dbsf(lanes, limit=1)
    top_two = fuse_dbsf(lanes, limit=2)
    top_five = fuse_dbsf(lanes, limit=5)

    assert [hit["chunk_id"] for hit in top_one] == ["c"]
    assert [hit["chunk_id"] for hit in top_two] == ["c", "b"]
    assert [hit["chunk_id"] for hit in top_five] == ["c", "b", "a", "e", "d"]
    two_value = distribution_scores((1.0, 2.0))
    assert [hit["score"] for hit in top_five] == pytest.approx(
        [4 / 3, 1.0, 2 / 3, two_value[1], two_value[0]]
    )
    assert tuple(top_five[0]) == ("chunk_id", "document_id", "score")
    tie = fuse_dbsf([[_hit("z", "doc-z", 1)], [_hit("a", "doc-a", 100)]], limit=1)
    assert tie == [{"chunk_id": "a", "document_id": "doc-a", "score": 0.5}]


def test_extreme_score_uses_unclipped_three_sigma_formula() -> None:
    scores = (-1000.0, *([0.0] * 73), 1000.0)
    mean = sum(scores) / len(scores)
    variance = sum((score - mean) ** 2 for score in scores) / (len(scores) - 1)
    standard_deviation = math.sqrt(variance)
    minimum = mean - 3 * standard_deviation
    maximum = mean + 3 * standard_deviation
    expected = tuple((score - minimum) / (maximum - minimum) for score in scores)

    assert distribution_scores(scores) == pytest.approx(expected)
    assert expected[0] < 0.0
    assert expected[-1] > 1.0


def test_empty_inputs_are_accepted() -> None:
    assert distribution_scores(()) == ()
    assert fuse_dbsf([], limit=1) == []
    assert fuse_dbsf([(), [], ()], limit=10) == []


def test_mapping_must_be_consistent_across_lanes_and_unique_within_lane() -> None:
    with pytest.raises(ValueError, match="conflicting document_id"):
        fuse_dbsf(
            [
                [_hit("chunk", "doc-a", 1)],
                [_hit("chunk", "doc-b", 2)],
            ],
            limit=10,
        )

    with pytest.raises(ValueError, match="duplicate chunk_id"):
        fuse_dbsf(
            [[_hit("chunk", "doc-a", 1), _hit("chunk", "doc-a", 2)]],
            limit=10,
        )


@pytest.mark.parametrize(
    "hit",
    [
        {"document_id": "doc", "score": 1.0},
        {"chunk_id": "chunk", "score": 1.0},
        _hit("", "doc", 1.0),
        _hit("   ", "doc", 1.0),
        _hit(None, "doc", 1.0),
        _hit("chunk", "", 1.0),
        _hit("chunk", "   ", 1.0),
        _hit("chunk", None, 1.0),
    ],
)
def test_missing_and_invalid_identifiers_are_rejected(hit: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        fuse_dbsf([[hit]], limit=10)


@pytest.mark.parametrize(
    "score",
    [None, True, False, "1.0", object(), math.nan, math.inf, -math.inf],
)
def test_bool_nonnumeric_and_nonfinite_scores_are_rejected(score: object) -> None:
    with pytest.raises(ValueError, match="finite number"):
        fuse_dbsf([[_hit("chunk", "doc", score)]], limit=10)

    with pytest.raises(ValueError, match="finite number"):
        distribution_scores((1.0, score))  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.0, "1", None])
def test_limit_must_be_a_strictly_positive_integer(limit: Any) -> None:
    with pytest.raises(ValueError, match="strictly positive integer"):
        fuse_dbsf([], limit=limit)


def test_inputs_are_not_mutated_and_extra_fields_are_not_projections() -> None:
    lanes = [
        [
            _hit("a", "doc-a", -3, rank=1, payload={"text": "left"}),
            _hit("b", "doc-b", 5, rank=2, payload={"text": "right"}),
        ],
        [_hit("a", "doc-a", 9, rank=1, payload={"text": "left"})],
    ]
    original = deepcopy(lanes)

    result = fuse_dbsf(lanes, limit=2)

    assert lanes == original
    assert all(set(hit) == {"chunk_id", "document_id", "score"} for hit in result)
    assert [hit["chunk_id"] for hit in result] == ["a", "b"]
