"""Unit tests for temporal validity and conditional scope memory governance."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.agent_runtime.memory_governance import (
    ScopedMemoryRecord,
    filter_active_memories,
    format_enhanced_memory_notes,
)


def test_filter_active_memories_drops_expired_and_out_of_scope_records() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    records = [
        ScopedMemoryRecord(
            memory_id="m-valid",
            kind="PREFERENCE",
            content="Prefer window seats on flights.",
        ),
        ScopedMemoryRecord(
            memory_id="m-expired",
            kind="CONSTRAINT",
            content="Send bank mail by email while abroad.",
            valid_until=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
        ),
        ScopedMemoryRecord(
            memory_id="m-family",
            kind="CONSTRAINT",
            content="Ask before booking flights over 1200 CNY.",
            condition="family travel",
            scope_tags=("family_travel",),
        ),
        ScopedMemoryRecord(
            memory_id="m-biz",
            kind="PREFERENCE",
            content="Up to 1250 CNY is acceptable for urgent business meetings.",
            condition="business travel",
            scope_tags=("business_travel",),
            valid_until=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            source_ref="thread-42",
        ),
    ]

    active, dropped = filter_active_memories(
        records,
        now=now,
        active_scope_tags=("business_travel",),
    )
    assert [r.memory_id for r in active] == ["m-valid", "m-biz"]
    assert dropped == {"m-expired": "EXPIRED", "m-family": "SCOPE_MISMATCH"}

    rendered = format_enhanced_memory_notes(active)
    assert "enhanced_notes_v1" in rendered
    assert "when: business travel" in rendered
    assert "valid_until: 2027-01-01" in rendered
    assert "source: thread-42" in rendered
