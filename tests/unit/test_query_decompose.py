"""P-C1 decomposer unit tests: strict schema, fallback semantics, pure rankers."""

from __future__ import annotations

import json

import pytest

from benchmarks.public_rag.query_decompose import (
    BM25Index,
    parse_decomposition,
    rrf_fuse_n,
    tokenize,
)


def test_parse_accepts_valid_decomposition_and_deduplicates() -> None:
    parsed = parse_decomposition(
        json.dumps({"sub_queries": ["Who wrote X?", "Who wrote X?", "When was Y born?"]})
    )
    assert parsed == ("Who wrote X?", "When was Y born?")


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"queries": ["a"]}),
        json.dumps({"sub_queries": ["a"], "extra": 1}),
        json.dumps({"sub_queries": []}),
        json.dumps({"sub_queries": ["a"] * 5}),
        json.dumps({"sub_queries": ["a", 2]}),
        json.dumps({"sub_queries": ["a", "   "]}),
        json.dumps({"sub_queries": ["a", "x" * 501]}),
        json.dumps({"sub_queries": "not-a-list"}),
    ],
)
def test_parse_rejects_every_deviation(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_decomposition(payload)


def test_single_sub_query_is_valid_no_decomposition_is_not_failure() -> None:
    assert parse_decomposition(json.dumps({"sub_queries": ["only"]})) == ("only",)


def test_tokenize_is_lowercase_words() -> None:
    assert tokenize("The Matrix (1999)!") == ("the", "matrix", "1999")


def test_bm25_ranks_the_matching_document_first() -> None:
    index = BM25Index(
        [
            "refund policy window seven days",
            "unrelated shipping carrier notice",
            "refund policy escalation premium members",
        ]
    )
    ranking = index.ranking("premium refund escalation")
    assert ranking[0] == 2
    assert sorted(ranking) == [0, 1, 2]


def test_rrf_fusion_promotes_documents_both_sub_queries_agree_on() -> None:
    # Two sub-query rankings disagree on top but agree on document 2.
    fused = rrf_fuse_n([[0, 1, 2], [1, 2, 0]])
    assert fused[0] == 1  # ranked high in both
    assert sorted(fused) == [0, 1, 2]


def test_rrf_fusion_with_one_ranking_is_that_ranking() -> None:
    assert rrf_fuse_n([[3, 1, 2]]) == [3, 1, 2]
