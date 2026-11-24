"""Temporal validity, conditional scope filtering, and Enhanced-Note formatting for Memory.

Implements Chapter 3's memory governance principles:
1. Temporal validity: memories with ``valid_until`` in the past are filtered out
   deterministically with an explicit audit reason (``EXPIRED``).
2. Conditional scope: memories tied to a specific context/scope tag (e.g.,
   ``business_travel`` vs ``family_travel``) are admitted only when their
   condition matches the active turn scope (or when unconditional).
3. Enhanced-Note formatting: preserves full relational context, condition, and
   validity window in one coherent note instead of fragmented key-value pairs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ScopedMemoryRecord:
    memory_id: str
    kind: str  # FACT | PREFERENCE | DECISION | CONSTRAINT
    content: str
    condition: str | None = None
    scope_tags: tuple[str, ...] = ()
    valid_until: datetime | None = None
    source_ref: str | None = None


def filter_active_memories(
    records: Sequence[ScopedMemoryRecord],
    *,
    now: datetime | None = None,
    active_scope_tags: Iterable[str] = (),
) -> tuple[tuple[ScopedMemoryRecord, ...], dict[str, str]]:
    """Filter out expired or out-of-scope memories and return drop reasons."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    active_tags = {tag.strip().lower() for tag in active_scope_tags if tag and tag.strip()}

    admitted: list[ScopedMemoryRecord] = []
    drop_reasons: dict[str, str] = {}

    for rec in records:
        if rec.valid_until is not None:
            expiry = (
                rec.valid_until.replace(tzinfo=UTC)
                if rec.valid_until.tzinfo is None
                else rec.valid_until.astimezone(UTC)
            )
            if expiry < current:
                drop_reasons[rec.memory_id] = "EXPIRED"
                continue
        if rec.scope_tags:
            req_tags = {t.strip().lower() for t in rec.scope_tags if t and t.strip()}
            if req_tags and not (req_tags & active_tags):
                drop_reasons[rec.memory_id] = "SCOPE_MISMATCH"
                continue
        admitted.append(rec)

    return tuple(admitted), drop_reasons


def format_enhanced_memory_notes(records: Sequence[ScopedMemoryRecord]) -> str:
    """Render Enhanced Notes preserving condition, validity window, and source."""
    if not records:
        return ""
    lines = ["<memory_notes format=\"enhanced_notes_v1\">"]
    for rec in records:
        meta_parts = [rec.kind]
        if rec.condition:
            meta_parts.append(f"when: {rec.condition}")
        if rec.valid_until is not None:
            meta_parts.append(f"valid_until: {rec.valid_until.date().isoformat()}")
        if rec.source_ref:
            meta_parts.append(f"source: {rec.source_ref}")
        header = " | ".join(meta_parts)
        lines.append(f"- [{header}] {rec.content}")
    lines.append("</memory_notes>")
    return "\n".join(lines)


__all__ = [
    "ScopedMemoryRecord",
    "filter_active_memories",
    "format_enhanced_memory_notes",
]
