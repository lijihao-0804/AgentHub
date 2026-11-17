"""Deterministic text chunking and logical chunk identifiers."""

from __future__ import annotations

import hashlib
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from operator import itemgetter
from typing import Any
from uuid import UUID

from packages.core.canonical.json_hash import canonical_json_hash
from packages.knowledge.parser import ParsedBlock, ParsedDocument


@dataclass(frozen=True)
class ChunkCandidate:
    chunk_id: str
    ordinal: int
    normalized_content_hash: str
    text: str
    locator: dict[str, Any]


def normalize_chunk_text(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def source_slice_locator(block: ParsedBlock, start: int, end: int) -> dict[str, Any]:
    """Project block-local raw offsets onto their source and intersect any cell geometry."""
    locator = dict(block.locator)
    existing = locator.get("char_start", 0)
    base = existing if type(existing) is int and existing >= 0 else 0
    locator.update(char_start=base + start, char_end=base + end)
    cells = locator.get("cells")
    if locator.get("type") == "table_row" and cells and (start > 0 or end < len(block.text)):
        # The parser emits source cells in reading order, with disjoint text ranges.
        first = bisect_right(cells, start, key=itemgetter("char_end"))
        last = bisect_left(cells, end, key=itemgetter("char_start"))
        hits = cells[first:last]
        locator["cells"] = hits
        if hits:
            locator.update(column_start=hits[0]["column"], column_end=hits[-1]["column_end"])
        else:
            # A serialized delimiter can lie between two original cells.
            locator.pop("column_start", None)
            locator.pop("column_end", None)
    return locator


def chunk_candidate(
    revision: UUID, ordinal: int, text: str, locator: dict[str, Any]
) -> ChunkCandidate:
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunk_id = canonical_json_hash(
        {
            "document_revision_id": str(revision),
            "ordinal": ordinal,
            "normalized_content_hash": content_hash,
        }
    )
    return ChunkCandidate(chunk_id, ordinal, content_hash, text, locator)


def _fixed_windows(
    block: ParsedBlock, size: int, overlap: int
) -> Iterator[tuple[str, dict[str, Any]]]:
    layout = block.locator.get("type") == "table_row" or "block_index" in block.locator
    # New layout and table locators refer to raw cell/block coordinates. Legacy
    # TXT/MD/PDF windows use the original normalized coordinate space and IDs.
    source = block.text if layout else normalize_chunk_text(block.text)
    start = 0
    while start < len(source):
        end = min(start + size, len(source))
        text = normalize_chunk_text(source[start:end])
        if layout:
            # A few Unicode scalars expand under NFC. Shrink this source
            # slice before choosing the next start so evidence is not skipped.
            while len(text) > size and end > start + 1:
                end -= 1
                text = normalize_chunk_text(source[start:end])
            if len(text) > size:
                raise ValueError("chunk size cannot contain a normalized source character")
        if text:
            if layout:
                left, right = start, end
                while left < right and source[left].isspace():
                    left += 1
                while right > left and source[right - 1].isspace():
                    right -= 1
                locator = source_slice_locator(block, left, right)
            elif block.locator.get("type") == "text_range":
                locator = source_slice_locator(block, start, end)
            else:
                locator = dict(block.locator)
            yield text, locator
        if end == len(source):
            break
        start = max(start + 1, end - overlap)


def build_deterministic_chunks(
    document_revision_id: UUID,
    parsed_document: ParsedDocument,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
) -> tuple[ChunkCandidate, ...]:
    if chunk_size_chars <= 0 or not 0 <= chunk_overlap_chars < chunk_size_chars:
        raise ValueError("chunk overlap must be smaller than a positive chunk size")

    candidates: list[ChunkCandidate] = []
    for block in parsed_document.blocks:
        for text, locator in _fixed_windows(block, chunk_size_chars, chunk_overlap_chars):
            candidates.append(chunk_candidate(document_revision_id, len(candidates), text, locator))
    return tuple(candidates)


__all__ = ["ChunkCandidate", "build_deterministic_chunks", "normalize_chunk_text"]
