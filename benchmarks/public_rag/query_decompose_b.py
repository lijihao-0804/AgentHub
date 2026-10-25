"""C1b stage 1: per-sub-query round-robin quota aggregation (zero API).

Each sub-query keeps its own BM25 in-case ranking; the interleave gives every
sub-query an equal claim on the top-5 slots before exact deduplication. This
is the only pre-registered mechanical variant; no quota-ratio or ordering
sweeps exist here by protocol.
"""

from __future__ import annotations

from collections.abc import Sequence


def round_robin_interleave(rankings: Sequence[Sequence[int]]) -> list[int]:
    """Interleave N rankings round-robin, dropping exact duplicates.

    Order: first elements of every ranking, then second elements, and so on.
    A document appears at its best interleaved position; later occurrences
    are dropped. Empty rankings contribute nothing; ties preserve the
    rankings' own order, so the output is deterministic for fixed input.
    """
    if not rankings:
        return []
    ordered: list[int] = []
    seen: set[int] = set()
    max_length = max(len(ranking) for ranking in rankings)
    for depth in range(max_length):
        for ranking in rankings:
            if depth >= len(ranking):
                continue
            document = ranking[depth]
            if document not in seen:
                seen.add(document)
                ordered.append(document)
    return ordered


def quota_top_k(rankings: Sequence[Sequence[int]], *, k: int) -> list[int]:
    """The C1b-1 delivery: interleaved, deduplicated, cut at ``k``."""
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    return round_robin_interleave(rankings)[:k]


__all__ = ["quota_top_k", "round_robin_interleave"]
