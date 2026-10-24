"""Byte-budget selection for ranked knowledge records."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def _list_json_bytes(records: Sequence[Mapping[str, Any]]) -> int:
    try:
        encoded = json.dumps(
            list(records),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("records must contain JSON-serializable values") from exc
    return len(encoded.encode("utf-8"))


def select_budgeted_records(
    records: Sequence[Mapping[str, Any]],
    *,
    max_bytes: int,
    max_items: int,
) -> tuple[Mapping[str, Any], ...]:
    """Select ranked records whose compact JSON list fits within ``max_bytes``.

    Records are returned unchanged and in source order. An oversized record is
    skipped so later records can use the remaining budget. This function bounds
    only this list's serialized size; callers must still enforce their complete
    prompt budget with ``ContextBudgetPolicy``.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 2:
        raise ValueError("max_bytes must be an integer of at least 2")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
        raise ValueError("max_items must be a positive integer")

    _list_json_bytes(records)
    selected: list[Mapping[str, Any]] = []
    for record in records:
        if len(selected) >= max_items:
            break
        candidate = [*selected, record]
        candidate_bytes = _list_json_bytes(candidate)
        if candidate_bytes <= max_bytes:
            selected.append(record)

    return tuple(selected)


__all__ = ["select_budgeted_records"]
