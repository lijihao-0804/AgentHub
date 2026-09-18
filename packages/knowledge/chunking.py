"""Deterministic text chunking and logical chunk identifiers."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.core.canonical.json_hash import canonical_json_hash
from packages.knowledge.parser import ParsedDocument


@dataclass(frozen=True)
class ChunkCandidate:
    chunk_id: str
    ordinal: int
    normalized_content_hash: str
    text: str
    locator: dict[str, Any]


def normalize_chunk_text(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def build_deterministic_chunks(
    document_revision_id: UUID,
    parsed_document: ParsedDocument,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
) -> tuple[ChunkCandidate, ...]:
    if chunk_size_chars <= 0 or not 0 <= chunk_overlap_chars < chunk_size_chars:
        raise ValueError("chunk overlap must be smaller than a positive chunk size")

    step = chunk_size_chars - chunk_overlap_chars
    candidates: list[ChunkCandidate] = []
    for block in parsed_document.blocks:
        normalized_block = normalize_chunk_text(block.text)
        if not normalized_block:
            continue
        start = 0
        while start < len(normalized_block):
            end = min(start + chunk_size_chars, len(normalized_block))
            text = normalize_chunk_text(normalized_block[start:end])
            if text:
                locator = dict(block.locator)
                if locator.get("type") == "text_range":
                    locator["char_start"] = start
                    locator["char_end"] = end
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                ordinal = len(candidates)
                chunk_id = canonical_json_hash(
                    {
                        "document_revision_id": str(document_revision_id),
                        "ordinal": ordinal,
                        "normalized_content_hash": content_hash,
                    }
                )
                candidates.append(
                    ChunkCandidate(
                        chunk_id=chunk_id,
                        ordinal=ordinal,
                        normalized_content_hash=content_hash,
                        text=text,
                        locator=locator,
                    )
                )
            if end == len(normalized_block):
                break
            start += step
    return tuple(candidates)


__all__ = ["ChunkCandidate", "build_deterministic_chunks", "normalize_chunk_text"]
