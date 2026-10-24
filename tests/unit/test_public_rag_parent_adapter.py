from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import pytest

from benchmarks.public_rag.parent_rerank_adapter import ParentContextReranker
from packages.knowledge.contracts import RerankCandidate


class _FakeReranker:
    def __init__(self, scores: tuple[float, ...]) -> None:
        self.scores = scores
        self.calls: list[tuple[str, tuple[RerankCandidate, ...]]] = []
        self.raw_candidates: list[Sequence[RerankCandidate]] = []
        self.warm_count = 0

    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> tuple[float, ...]:
        self.raw_candidates.append(candidates)
        pairs = tuple(candidates)
        self.calls.append((query, pairs))
        return self.scores

    def warm(self) -> None:
        self.warm_count += 1


def _candidate(chunk_id: str, text: str) -> RerankCandidate:
    return RerankCandidate(chunk_id, text)


def test_chunk_mode_forwards_the_same_pairs_and_scores() -> None:
    candidates = (
        _candidate("child-1", "first child"),
        _candidate("child-2", "second child"),
    )
    delegate = _FakeReranker((0.25, 0.75))
    adapter = ParentContextReranker(
        delegate,
        parents={
            "child-1": ("parent-1", "unused parent"),
            "child-2": ("parent-2", "another unused parent"),
        },
        mode="chunk",
    )

    scores = adapter.rerank("query", candidates)

    assert scores == delegate.scores
    assert delegate.calls == [("query", candidates)]
    assert delegate.raw_candidates[0] is candidates
    assert adapter.last_pairs is candidates
    assert adapter.last_stats == {
        "input_chunks": 2,
        "neural_pairs": 2,
        "input_characters": len("first child") + len("second child"),
        "expanded_parent_count": 0,
    }


def test_chunk_mode_preserves_the_callers_sequence_identity() -> None:
    candidates = [_candidate("child-1", "first child")]
    delegate = _FakeReranker((0.25,))
    adapter = ParentContextReranker(
        delegate,
        parents={"child-1": ("parent-1", "parent text")},
        mode="chunk",
    )

    adapter.rerank("query", candidates)

    assert delegate.raw_candidates[0] is candidates
    assert adapter.last_pairs == tuple(candidates)


def test_parent_mode_uses_first_parent_order_and_broadcasts_scores_to_children() -> None:
    candidates = (
        _candidate("child-a", "alpha"),
        _candidate("child-b", "beta"),
        _candidate("child-c", "gamma"),
        _candidate("child-d", "delta"),
    )
    delegate = _FakeReranker((0.2, 0.5, 0.9))
    adapter = ParentContextReranker(
        delegate,
        parents={
            "child-a": ("parent-1", "the complete first parent"),
            "child-b": ("parent-2", "beta"),
            "child-c": ("parent-1", "the complete first parent"),
            "child-d": ("parent-3", "complete third parent"),
        },
        mode="parent",
    )

    scores = adapter.rerank("query", candidates)

    expected_pairs = (
        _candidate("parent-1", "the complete first parent"),
        _candidate("parent-2", "beta"),
        _candidate("parent-3", "complete third parent"),
    )
    assert scores == (0.2, 0.5, 0.2, 0.9)
    assert delegate.calls == [("query", expected_pairs)]
    assert adapter.last_pairs is delegate.calls[0][1]
    assert adapter.last_stats == {
        "input_chunks": 4,
        "neural_pairs": 3,
        "input_characters": sum(len(pair.text) for pair in expected_pairs),
        "expanded_parent_count": 2,
    }


@pytest.mark.parametrize("mode", ["chunk", "parent"])
def test_unknown_child_fails_before_inference_in_both_modes(mode: str) -> None:
    delegate = _FakeReranker((0.5,))
    adapter = ParentContextReranker(
        delegate,
        parents={"known": ("parent-1", "parent text")},
        mode=mode,
    )

    with pytest.raises(ValueError, match="no authorized parent mapping"):
        adapter.rerank("query", (_candidate("unknown", "candidate text"),))

    assert delegate.calls == []
    assert adapter.last_stats == {}
    assert adapter.last_pairs == ()


@pytest.mark.parametrize("mode", ["chunk", "parent"])
def test_duplicate_children_fail_before_inference_in_both_modes(mode: str) -> None:
    delegate = _FakeReranker((0.5,))
    adapter = ParentContextReranker(
        delegate,
        parents={"child-1": ("parent-1", "parent text")},
        mode=mode,
    )

    with pytest.raises(ValueError, match="duplicate child chunk IDs"):
        adapter.rerank(
            "query",
            (
                _candidate("child-1", "first"),
                _candidate("child-1", "second"),
            ),
        )

    assert delegate.calls == []


@pytest.mark.parametrize(
    ("scores", "message"),
    [
        ((0.5,), "returned 1 scores for 2 pairs"),
        ((0.5, math.nan), "must be finite"),
        ((0.5, math.inf), "must be finite"),
        ((0.5, "not-a-number"), "must be finite"),
    ],
)
def test_invalid_scores_clear_previous_audit_state(
    scores: tuple[float, ...],
    message: str,
) -> None:
    delegate = _FakeReranker((0.1, 0.9))
    candidates = (
        _candidate("child-1", "first"),
        _candidate("child-2", "second"),
    )
    adapter = ParentContextReranker(
        delegate,
        parents={
            "child-1": ("parent-1", "parent one"),
            "child-2": ("parent-2", "parent two"),
        },
        mode="parent",
    )
    assert adapter.rerank("query", candidates) == (0.1, 0.9)
    assert adapter.last_stats

    delegate.scores = scores
    with pytest.raises(ValueError, match=message):
        adapter.rerank("query", candidates)

    assert adapter.last_stats == {}
    assert adapter.last_pairs == ()


@pytest.mark.parametrize("mode", ["chunk", "parent"])
def test_empty_candidates_succeed_without_delegating(mode: str) -> None:
    delegate = _FakeReranker((0.5,))
    adapter = ParentContextReranker(delegate, parents={}, mode=mode)

    assert adapter.rerank("query", ()) == ()

    assert delegate.calls == []
    assert adapter.last_pairs == ()
    assert adapter.last_stats == {
        "input_chunks": 0,
        "neural_pairs": 0,
        "input_characters": 0,
        "expanded_parent_count": 0,
    }


def test_construction_copies_and_freezes_the_authorized_parent_mapping() -> None:
    source = {"child-1": ("parent-1", "original parent")}
    delegate = _FakeReranker((0.5,))
    adapter = ParentContextReranker(delegate, parents=source, mode="parent")
    source["child-1"] = ("parent-2", "replacement parent")

    adapter.rerank("query", (_candidate("child-1", "child text"),))

    assert delegate.calls[-1][1] == (_candidate("parent-1", "original parent"),)
    with pytest.raises(TypeError):
        adapter.parents["child-2"] = ("parent-2", "new parent")  # type: ignore[index]


def test_construction_rejects_conflicting_texts_for_the_same_parent_id() -> None:
    with pytest.raises(ValueError, match="conflicting text"):
        ParentContextReranker(
            _FakeReranker(()),
            parents={
                "child-1": ("parent-1", "first text"),
                "child-2": ("parent-1", "different text"),
            },
            mode="parent",
        )


@pytest.mark.parametrize(
    "parent",
    [
        ("", "parent text"),
        ("   ", "parent text"),
        (None, "parent text"),
        ("parent-1", ""),
        ("parent-1", "   "),
        ("parent-1", None),
        ("parent-1",),
    ],
)
def test_construction_rejects_invalid_parent_values(parent: Any) -> None:
    with pytest.raises(ValueError):
        ParentContextReranker(
            _FakeReranker(()),
            parents={"child-1": parent},
            mode="parent",
        )


@pytest.mark.parametrize("mode", ["", "chunks", "PARENT", None])
def test_construction_rejects_unknown_modes(mode: Any) -> None:
    with pytest.raises(ValueError, match="mode must be"):
        ParentContextReranker(_FakeReranker(()), parents={}, mode=mode)


def test_warm_forwards_only_when_the_delegate_supports_it() -> None:
    delegate = _FakeReranker(())
    ParentContextReranker(delegate, parents={}, mode="chunk").warm()

    class _NoWarmDelegate:
        def rerank(
            self,
            query: str,
            candidates: Sequence[RerankCandidate],
        ) -> tuple[float, ...]:
            return ()

    ParentContextReranker(_NoWarmDelegate(), parents={}, mode="chunk").warm()

    assert delegate.warm_count == 1
