"""Shared source-record projection before model-facing evidence assembly."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from packages.knowledge.contracts import RetrievedEvidence

# Legacy Wiki/citation excerpt limit. Raw search records retain the complete chunk.
MAX_EVIDENCE_EXCERPT_CHARS = 2_000


def evidence_record(item: RetrievedEvidence, *, snapshot_id: str | None = None) -> dict[str, Any]:
    # Identity fields (chunk_id, document_revision_id) stay because projection
    # and registry pairing key on them; the model-irrelevant provenance string
    # and the two ranking scores were measured to cost ~180 bytes per record
    # of pure metadata overhead against a 5000-byte evidence pool.
    record = {
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "document_revision_id": item.document_revision_id,
        "locator": dict(item.locator),
        "snippet": item.text,
        "document_name": item.metadata.get("document_name"),
        "effective_date": item.metadata.get("effective_date"),
        "superseded": bool(item.metadata.get("superseded", False)),
    }
    if snapshot_id is not None:
        record["snapshot_id"] = snapshot_id
    return record


def evidence_reference(item: RetrievedEvidence, *, snapshot_id: str, knowledge_base_id: str):
    return {
        "snapshot_id": snapshot_id,
        "knowledge_base_id": knowledge_base_id,
        "document_revision_id": item.document_revision_id,
        "chunk_id": item.chunk_id,
    }


def _locator_range(locator: Any) -> tuple[str, tuple[int, int]] | None:
    if not isinstance(locator, Mapping):
        return None
    for start_key, end_key in (("char_start", "char_end"), ("start", "end")):
        start, end = locator.get(start_key), locator.get(end_key)
        if type(start) is int and type(end) is int and 0 <= start < end:
            return start_key, (start, end)
    return None


def locator_span(locator: Any) -> tuple[int, int] | None:
    """Return the offset pair as written, preferring explicit character offsets."""
    located = _locator_range(locator)
    return located[1] if located is not None else None


def _scoped_span(revision_id: Any, locator: Any) -> tuple[tuple, tuple[int, int]] | None:
    located = _locator_range(locator)
    if located is None or not isinstance(revision_id, str) or not revision_id:
        return None
    # Offsets restart on each page, paragraph, layout block, visual part or table
    # row. A line number, paragraph-local offset and document character offset can
    # also have the same value while addressing different source text.
    locator_type = locator.get("type")
    if locator_type not in (
        None,
        "page",
        "pdf_page",
        "text_range",
        "table_row",
        "line_range",
        "paragraph_index",
    ):
        return None
    coordinates = ("page", "paragraph_ordinal", "block_index", "part_index", "row", "index")
    scope = tuple(locator.get(key) for key in coordinates)
    if any(value is not None and type(value) is not int for value in scope):
        return None
    basis, span = located
    return (revision_id, locator_type, basis, *scope), span


def _span_contained(span: tuple[int, int], kept: tuple[int, int]) -> bool:
    return span[0] >= kept[0] and span[1] <= kept[1]


@dataclass(frozen=True)
class EvidenceTextBudget:
    """Consumer budget for the body of each complete source record."""

    limit: int
    estimate: Callable[[str], int]


class EvidenceAdmission:
    """Track coverage and an optional body budget for one ranked selection."""

    def __init__(self, text_budget: EvidenceTextBudget | None = None) -> None:
        self._spans: dict[tuple, list[tuple[int, int]]] = {}
        self._text_budget = text_budget
        self._used_units = 0
        self.dropped_overlaps = 0

    def skip_reason(
        self, revision_id: Any, locator: Any, text: str, reason: str | None = None
    ) -> str | None:
        """Record coverage only when the caller's count and byte checks also pass."""
        location = _scoped_span(revision_id, locator)
        if location is not None:
            scope, span = location
            if any(_span_contained(span, other) for other in self._spans.get(scope, ())):
                self.dropped_overlaps += 1
                return "overlap"
        if reason is not None:
            return reason
        if self._text_budget is not None:
            cost = self._text_budget.estimate(text)
            if self._used_units + cost > self._text_budget.limit:
                return "budget"
            self._used_units += cost
        if location is not None:
            self._spans.setdefault(scope, []).append(span)
        return None

    def record_skip_reason(self, record: Mapping, reason: str | None = None) -> str | None:
        return self.skip_reason(
            record.get("document_revision_id"),
            record.get("locator"),
            record.get("snippet", record.get("text", "")),
            reason,
        )


def drop_overlapping_evidence(
    items: Sequence[RetrievedEvidence],
    *,
    max_items: int | None = None,
    text_budget: EvidenceTextBudget | None = None,
) -> tuple[RetrievedEvidence, ...]:
    """Select complete ranked records within the caller's count and body budget.

    A kept span suppresses later spans contained in the same source coordinate
    space. Partial overlaps and records without comparable offsets remain eligible.
    """
    admission = EvidenceAdmission(text_budget)
    kept: list[RetrievedEvidence] = []
    for item in items:
        if max_items is not None and len(kept) >= max_items:
            break
        if admission.skip_reason(item.document_revision_id, item.locator, item.text) is not None:
            continue
        kept.append(item)
    return tuple(kept)


def drop_overlapping_records(
    records: Sequence[Any], references: Sequence[Any]
) -> tuple[list[Any], list[Any]]:
    """Pair-preserving overlap filter for record/reference projection inputs.

    Malformed shapes (mismatched lengths, non-mapping records) are returned
    unchanged so the downstream projection still fails closed on them.
    """
    if len(records) != len(references):
        return list(records), list(references)
    admission = EvidenceAdmission()
    seen: set[tuple[str, str]] = set()
    kept_records: list[Any] = []
    kept_references: list[Any] = []
    for record, reference in zip(records, references, strict=True):
        if not isinstance(record, Mapping):
            return list(records), list(references)
        identity = (record.get("document_revision_id"), record.get("chunk_id"))
        if all(isinstance(value, str) and value for value in identity):
            if identity in seen:
                continue
            seen.add(identity)
        if admission.record_skip_reason(record) is not None:
            continue
        kept_records.append(record)
        kept_references.append(reference)
    return kept_records, kept_references


__all__ = [
    "EvidenceAdmission",
    "EvidenceTextBudget",
    "MAX_EVIDENCE_EXCERPT_CHARS",
    "drop_overlapping_evidence",
    "drop_overlapping_records",
    "evidence_record",
    "evidence_reference",
    "locator_span",
]
