"""Byte-budget selection for ranked knowledge records."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def _record_json_bytes(record: Mapping[str, Any]) -> int:
    try:
        encoded = json.dumps(
            record,
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

    # A compact JSON list is exactly "[" + ",".join(items) + "]", so the list
    # size grows incrementally instead of re-serializing the whole selection
    # for every candidate.
    item_bytes = [_record_json_bytes(record) for record in records]
    selected: list[Mapping[str, Any]] = []
    used_bytes = 2
    for record, cost in zip(records, item_bytes, strict=True):
        if len(selected) >= max_items:
            break
        separator = 1 if selected else 0
        if used_bytes + separator + cost <= max_bytes:
            selected.append(record)
            used_bytes += separator + cost

    return tuple(selected)


__all__ = ["select_budgeted_records"]
