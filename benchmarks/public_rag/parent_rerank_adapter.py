"""Benchmark-only reranking over authorized parent contexts."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType

from packages.knowledge.contracts import RerankCandidate, Reranker


class ParentContextReranker:
    """Rerank chunks directly or through their snapshot-scoped parent texts."""

    def __init__(
        self,
        delegate: Reranker,
        *,
        parents: Mapping[str, tuple[str, str]],
        mode: str,
    ) -> None:
        if mode not in {"chunk", "parent"}:
            raise ValueError("mode must be 'chunk' or 'parent'")

        copied_parents: dict[str, tuple[str, str]] = {}
        parent_id_by_child: dict[str, str] = {}
        parent_text_by_id: dict[str, str] = {}
        for child_id, parent in parents.items():
            self._require_nonempty_string(child_id, "child chunk ID")
            if not isinstance(parent, tuple) or len(parent) != 2:
                raise ValueError("each parent mapping value must be a (parent ID, text) tuple")
            parent_id, parent_text = parent
            self._require_nonempty_string(parent_id, "parent ID")
            self._require_nonempty_string(parent_text, "parent text")

            existing_text = parent_text_by_id.get(parent_id)
            if existing_text is not None and existing_text != parent_text:
                raise ValueError(f"conflicting text for parent ID {parent_id!r}")

            copied_parents[child_id] = (parent_id, parent_text)
            parent_id_by_child[child_id] = parent_id
            parent_text_by_id[parent_id] = parent_text

        self.delegate = delegate
        self.mode = mode
        self.parents: Mapping[str, tuple[str, str]] = MappingProxyType(copied_parents)
        self._parent_id_by_child: Mapping[str, str] = MappingProxyType(parent_id_by_child)
        self._parent_text_by_id: Mapping[str, str] = MappingProxyType(parent_text_by_id)
        self.last_stats: dict[str, int] = {}
        self.last_pairs: tuple[RerankCandidate, ...] = ()

    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
    ) -> tuple[float, ...]:
        self._clear_audit()
        input_candidates = tuple(candidates)
        child_ids = tuple(candidate.chunk_id for candidate in input_candidates)
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("duplicate child chunk IDs are not supported")

        for child_id in child_ids:
            if child_id not in self._parent_id_by_child:
                raise ValueError(f"no authorized parent mapping for child chunk {child_id!r}")

        if not input_candidates:
            self.last_stats = {
                "input_chunks": 0,
                "neural_pairs": 0,
                "input_characters": 0,
                "expanded_parent_count": 0,
            }
            return ()

        if self.mode == "chunk":
            pairs = input_candidates
            parent_index: Mapping[str, int] = {}
        else:
            parent_order: list[str] = []
            seen_parent_ids: set[str] = set()
            for child_id in child_ids:
                parent_id = self._parent_id_by_child[child_id]
                if parent_id not in seen_parent_ids:
                    seen_parent_ids.add(parent_id)
                    parent_order.append(parent_id)
            pairs = tuple(
                RerankCandidate(parent_id, self._parent_text_by_id[parent_id])
                for parent_id in parent_order
            )
            parent_index = MappingProxyType(
                {parent_id: index for index, parent_id in enumerate(parent_order)}
            )

        delegate_pairs: Sequence[RerankCandidate] = candidates if self.mode == "chunk" else pairs
        raw_scores = self.delegate.rerank(query, delegate_pairs)
        scores = self._validate_scores(raw_scores, expected=len(pairs))

        if self.mode == "chunk":
            output_scores = scores
            expanded_parent_count = 0
        else:
            output_scores = tuple(
                scores[parent_index[self._parent_id_by_child[child_id]]] for child_id in child_ids
            )
            max_child_characters: dict[str, int] = {}
            for candidate in input_candidates:
                parent_id = self._parent_id_by_child[candidate.chunk_id]
                max_child_characters[parent_id] = max(
                    max_child_characters.get(parent_id, 0),
                    len(candidate.text),
                )
            expanded_parent_count = sum(
                len(self._parent_text_by_id[parent_id]) > max_child_characters[parent_id]
                for parent_id in parent_index
            )

        self.last_pairs = pairs
        self.last_stats = {
            "input_chunks": len(input_candidates),
            "neural_pairs": len(pairs),
            "input_characters": sum(len(pair.text) for pair in pairs),
            "expanded_parent_count": expanded_parent_count,
        }
        return output_scores

    def warm(self) -> None:
        warm = getattr(self.delegate, "warm", None)
        if callable(warm):
            warm()

    def _clear_audit(self) -> None:
        self.last_stats = {}
        self.last_pairs = ()

    @staticmethod
    def _require_nonempty_string(value: object, label: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")

    @staticmethod
    def _validate_scores(
        raw_scores: Iterable[float],
        *,
        expected: int,
    ) -> tuple[float, ...]:
        try:
            scores = tuple(raw_scores)
        except TypeError as exc:
            raise ValueError("reranker scores must be iterable") from exc
        if len(scores) != expected:
            raise ValueError(f"reranker returned {len(scores)} scores for {expected} pairs")
        try:
            finite = all(math.isfinite(score) for score in scores)
        except (TypeError, ValueError) as exc:
            raise ValueError("reranker scores must be finite numbers") from exc
        if not finite:
            raise ValueError("reranker scores must be finite numbers")
        return scores
