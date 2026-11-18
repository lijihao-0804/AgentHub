"""Join at most two hydrated snapshot chunks with provably shared source coordinates."""

from dataclasses import dataclass, replace
from itertools import combinations

from packages.knowledge.contracts import RetrievedEvidence


@dataclass(frozen=True)
class CitationComponent:
    chunk_id: str
    locator: dict
    excerpt: str


def _range(item: RetrievedEvidence) -> tuple[int, int] | None:
    locator = item.locator
    # Plain-text offsets share one document coordinate space. PDF layout offsets
    # are block-relative and need a page and block identity. Cell/table geometry
    # and locators with ambiguous units are deliberately ineligible.
    if locator.get("type") != "text_range" and not (
        locator.get("type") == "page"
        and type(locator.get("page")) is int
        and type(locator.get("block_index")) is int
    ):
        return None
    start, end = locator.get("char_start"), locator.get("char_end")
    if (
        type(start) is int
        and type(end) is int
        and 0 <= start < end
        and end - start == len(item.text)
    ):
        return start, end
    return None


def _join(left: RetrievedEvidence, right: RetrievedEvidence):
    if (
        not left.document_id
        or not left.document_revision_id
        or left.document_id != right.document_id
        or left.document_revision_id != right.document_revision_id
        or left.source != right.source
        or left.chunk_id == right.chunk_id
    ):
        return None
    a, b = _range(left), _range(right)
    if a is None or b is None:
        return None
    offsets = {"char_start", "char_end"}
    if {k: v for k, v in left.locator.items() if k not in offsets} != {
        k: v for k, v in right.locator.items() if k not in offsets
    }:
        return None
    first, last = left, right
    if b < a:
        first, last, a, b = right, left, b, a
    overlap = min(a[1], b[1]) - b[0]
    if overlap < 0 or first.text[b[0] - a[0] : b[0] - a[0] + overlap] != last.text[:overlap]:
        return None
    parts = (first, last)
    locator = {
        "type": "source_union",
        "char_start": a[0],
        "char_end": max(a[1], b[1]),
        "components": [{"chunk_id": p.chunk_id, "locator": p.locator} for p in parts],
    }
    return replace(left, text=first.text + last.text[overlap:], locator=locator), tuple(
        CitationComponent(p.chunk_id, dict(p.locator), p.text) for p in parts
    )


def propose_source_refill(ranked: tuple[RetrievedEvidence, ...]):
    original = ranked[:6]
    sources = {item.document_id for item in original}
    refill = next((i for i in ranked[6:20] if i.document_id not in sources), None)
    if refill is None:
        return None
    for i, j in combinations(range(len(original)), 2):
        joined = _join(original[i], original[j])
        if joined is not None:
            union, components = joined
            records = tuple(union if k == i else v for k, v in enumerate(original) if k != j)
            return (*records, refill), union.chunk_id, components
    return None
