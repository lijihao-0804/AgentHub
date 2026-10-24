"""Shared source-record projection before model-facing evidence assembly."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.knowledge.contracts import RetrievedEvidence

# The excerpt cap bounds the tool return and the private evidence registry, not
# the model view: the observation projection is lossless and drops whole
# records instead of truncating them. It must therefore stay at or above the
# configured chunk size (default 1200 chars) so a whole chunk can survive this
# cap; chunks larger than the cap lose only their tail here.
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
        "snippet": item.text[:MAX_EVIDENCE_EXCERPT_CHARS],
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


def locator_span(locator: Any) -> tuple[int, int] | None:
    """Return comparable character coordinates, or None when unavailable.

    ``char_start``/``char_end`` are document-absolute; ``start``/``end`` are the
    paragraph-relative pair some sources emit. Records without usable text
    coordinates (for example page-only PDF locators) are never deduplicated.
    """
    if not isinstance(locator, Mapping):
        return None
    for start_key, end_key in (("char_start", "char_end"), ("start", "end")):
        start, end = locator.get(start_key), locator.get(end_key)
        if type(start) is int and type(end) is int and 0 <= start < end:
            return start, end
    return None


def _span_contained(span: tuple[int, int], kept: tuple[int, int]) -> bool:
    return span[0] >= kept[0] and span[1] <= kept[1]


class _OverlapTracker:
    """Keeps records whose span is not fully covered by a kept record.

    The judgment is containment, not mere overlap: a fixed-window neighbour
    ([0,1200] then [1000,2200]) carries 1000 characters no kept record
    covers, so dropping it on overlap alone would delete unique content and
    leave an 800-character gap in a consecutive window chain. Only spans
    entirely inside an already-kept span (exact re-deliveries, tail windows)
    are redundant.
    """

    def __init__(self) -> None:
        self._spans: dict[str, list[tuple[int, int]]] = {}

    def redundant(self, revision_id: Any, span: tuple[int, int] | None) -> bool:
        if span is None or not isinstance(revision_id, str) or not revision_id:
            return False
        return any(_span_contained(span, other) for other in self._spans.get(revision_id, ()))

    def keep(self, revision_id: Any, span: tuple[int, int] | None) -> None:
        if span is None or not isinstance(revision_id, str) or not revision_id:
            return
        self._spans.setdefault(revision_id, []).append(span)


def drop_overlapping_evidence(
    items: Sequence[RetrievedEvidence],
) -> tuple[RetrievedEvidence, ...]:
    """Drop later evidence whose char span is fully contained in a kept span
    of the same document revision.

    Exact re-deliveries (multi-snapshot unions, tail windows inside a larger
    window) pay the budget twice for text a kept record already covers. A
    merely overlapping neighbour always carries unique text and is kept.
    Records without usable text coordinates are passed through unchanged so
    page-located sources keep their historical behaviour.
    """
    tracker = _OverlapTracker()
    kept: list[RetrievedEvidence] = []
    for item in items:
        span = locator_span(item.locator)
        if tracker.redundant(item.document_revision_id, span):
            continue
        tracker.keep(item.document_revision_id, span)
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
    tracker = _OverlapTracker()
    kept_records: list[Any] = []
    kept_references: list[Any] = []
    for record, reference in zip(records, references, strict=True):
        if not isinstance(record, Mapping):
            return list(records), list(references)
        span = locator_span(record.get("locator"))
        if tracker.redundant(record.get("document_revision_id"), span):
            continue
        tracker.keep(record.get("document_revision_id"), span)
        kept_records.append(record)
        kept_references.append(reference)
    return kept_records, kept_references


__all__ = [
    "MAX_EVIDENCE_EXCERPT_CHARS",
    "drop_overlapping_evidence",
    "drop_overlapping_records",
    "evidence_record",
    "evidence_reference",
    "locator_span",
]
