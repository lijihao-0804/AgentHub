"""Pure benchmark adapter for Qdrant DBSF score fusion.

This mirrors Qdrant ``222b2e44`` behavior using Python float64 arithmetic.
It is a behavioral benchmark adapter, not a bitwise Rust f32 equivalence claim.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def distribution_scores(scores: Sequence[float]) -> tuple[float, ...]:
    """Normalize one lane with Qdrant's Welford sample-distribution rule."""

    values = tuple(_finite_score(score, "score") for score in scores)
    if len(values) < 2:
        return tuple(0.5 for _ in values)

    mean = 0.0
    squared_deviations = 0.0
    for count, value in enumerate(values, start=1):
        delta = value - mean
        mean += delta / count
        squared_deviations += delta * (value - mean)

    variance = squared_deviations / (len(values) - 1)
    if variance == 0.0:
        return tuple(0.5 for _ in values)

    standard_deviation = math.sqrt(variance)
    minimum = mean - 3.0 * standard_deviation
    maximum = mean + 3.0 * standard_deviation
    width = maximum - minimum
    return tuple((value - minimum) / width for value in values)


def fuse_dbsf(
    lanes: Sequence[Sequence[Mapping[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fuse validated single-query lanes and return only the requested fields."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a strictly positive integer")

    document_by_chunk: dict[str, str] = {}
    fused_scores: dict[str, float] = {}
    for lane_index, lane in enumerate(lanes, start=1):
        lane_chunks: list[str] = []
        lane_scores: list[float] = []
        seen_chunks: set[str] = set()

        for hit in lane:
            if not isinstance(hit, Mapping):
                raise ValueError(f"lane {lane_index} hit must be a mapping")
            chunk_id = _identifier(hit, "chunk_id", lane_index)
            document_id = _identifier(hit, "document_id", lane_index)
            score_value = hit.get("score", _MISSING)
            if score_value is _MISSING:
                raise ValueError(f"lane {lane_index} hit is missing score")
            score = _finite_score(score_value, f"score for {chunk_id!r}")

            if chunk_id in seen_chunks:
                raise ValueError(f"duplicate chunk_id {chunk_id!r} in lane {lane_index}")
            seen_chunks.add(chunk_id)

            previous_document = document_by_chunk.get(chunk_id)
            if previous_document is not None and previous_document != document_id:
                raise ValueError(f"conflicting document_id for chunk_id {chunk_id!r}")

            document_by_chunk[chunk_id] = document_id
            lane_chunks.append(chunk_id)
            lane_scores.append(score)

        for chunk_id, score in zip(lane_chunks, distribution_scores(lane_scores), strict=True):
            fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + score

    ranked = sorted(fused_scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        {
            "chunk_id": chunk_id,
            "document_id": document_by_chunk[chunk_id],
            "score": score,
        }
        for chunk_id, score in ranked
    ]


_MISSING = object()


def _identifier(hit: Mapping[str, Any], field: str, lane_index: int) -> str:
    value = hit.get(field, _MISSING)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"lane {lane_index} hit {field} must be a non-empty string")
    return value


def _finite_score(value: object, label: str) -> float:
    if isinstance(value, (bool, str, bytes, bytearray)):
        raise ValueError(f"{label} must be a finite number")
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(score):
        raise ValueError(f"{label} must be a finite number")
    return score


__all__ = ["distribution_scores", "fuse_dbsf"]
