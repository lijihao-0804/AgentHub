"""Structure-aware, deterministic chunking over parsed source blocks.

Locator character offsets use each block's raw coordinate space. For
``text_range`` blocks, the existing ``char_start`` is the base offset and all
emitted ranges are relative to that base. Other locator types use block-local
offsets from zero. NFC and newline normalization affect only candidate text;
they never change the locator coordinate space.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.core.canonical.json_hash import canonical_json_hash
from packages.knowledge.chunking import ChunkCandidate, normalize_chunk_text
from packages.knowledge.parser import ParsedBlock, ParsedDocument

_PARAGRAPH_BREAK = re.compile(r"(?:\r\n|\r|\n)[ \t]*(?:\r\n|\r|\n)")
_MARKDOWN_HEADING = re.compile(r"[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
_WHITESPACE = re.compile(r"\s+")
_CHINESE_SENTENCE_TERMINALS = frozenset("。！？")
_ENGLISH_SENTENCE_TERMINALS = frozenset(".!?")
_CLOSING_PUNCTUATION = frozenset("\"'”’」』】）》〉")


@dataclass(frozen=True)
class _Span:
    start: int
    end: int

    @property
    def empty(self) -> bool:
        return self.start >= self.end


def build_structural_chunks(
    document_revision_id: UUID,
    parsed_document: ParsedDocument,
    *,
    chunk_size_chars: int = 1_000,
    chunk_overlap_chars: int = 0,
) -> tuple[ChunkCandidate, ...]:
    """Build non-overlapping chunks along natural reading units.

    Paragraphs are preserved as independent units when they fit. A markdown
    heading is attached to the following paragraph, or its first fitting
    bounded part, so it is not emitted as an orphan. Oversized paragraphs use
    English and Chinese terminal punctuation as sentence boundaries. A single
    oversized sentence falls back to whitespace and finally character splits.
    """

    if type(chunk_size_chars) is not int or chunk_size_chars <= 0:
        raise ValueError("chunk_size_chars must be a positive integer")
    if type(chunk_overlap_chars) is not int or chunk_overlap_chars != 0:
        raise ValueError("nonzero chunk overlap is unsupported")

    candidates: list[ChunkCandidate] = []
    for block in parsed_document.blocks:
        _append_block_candidates(
            candidates,
            document_revision_id=document_revision_id,
            block=block,
            chunk_size_chars=chunk_size_chars,
        )
    return tuple(candidates)


def _append_block_candidates(
    candidates: list[ChunkCandidate],
    *,
    document_revision_id: UUID,
    block: ParsedBlock,
    chunk_size_chars: int,
) -> None:
    paragraphs = _paragraph_spans(block.text)
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if not _is_markdown_heading(block.text, paragraph):
            _append_paragraph_pieces(
                candidates,
                document_revision_id=document_revision_id,
                block=block,
                span=paragraph,
                chunk_size_chars=chunk_size_chars,
            )
            index += 1
            continue

        heading_end_index = index
        heading_end = paragraph.end
        while heading_end_index + 1 < len(paragraphs) and _is_markdown_heading(
            block.text, paragraphs[heading_end_index + 1]
        ):
            next_heading = paragraphs[heading_end_index + 1]
            combined = _trim_span(block.text, paragraph.start, next_heading.end)
            if _normalized_length(block.text, combined) > chunk_size_chars:
                break
            heading_end_index += 1
            heading_end = next_heading.end

        body_index = heading_end_index + 1
        if body_index >= len(paragraphs):
            _append_paragraph_pieces(
                candidates,
                document_revision_id=document_revision_id,
                block=block,
                span=_Span(paragraph.start, heading_end),
                chunk_size_chars=chunk_size_chars,
            )
            index = body_index
            continue

        body = paragraphs[body_index]
        combined = _trim_span(block.text, paragraph.start, body.end)
        if _normalized_length(block.text, combined) <= chunk_size_chars:
            _append_candidate(
                candidates,
                document_revision_id=document_revision_id,
                block=block,
                span=combined,
                chunk_size_chars=chunk_size_chars,
            )
            index = body_index + 1
            continue

        heading = _trim_span(block.text, paragraph.start, heading_end)
        if _normalized_length(block.text, heading) <= chunk_size_chars:
            safe_end = _largest_hard_prefix_end(
                block.text, heading.start, body.end, chunk_size_chars
            )
            attach_end = _preferred_prefix_end(
                block.text,
                heading.start,
                safe_end,
                minimum_content_start=body.start + 1,
            )
            attached = _trim_span(block.text, heading.start, attach_end)
            if attached.end > body.start:
                _append_candidate(
                    candidates,
                    document_revision_id=document_revision_id,
                    block=block,
                    span=attached,
                    chunk_size_chars=chunk_size_chars,
                )
                remainder = _trim_span(block.text, attached.end, body.end)
                if not remainder.empty:
                    _append_paragraph_pieces(
                        candidates,
                        document_revision_id=document_revision_id,
                        block=block,
                        span=remainder,
                        chunk_size_chars=chunk_size_chars,
                    )
                index = body_index + 1
                continue

        _append_paragraph_pieces(
            candidates,
            document_revision_id=document_revision_id,
            block=block,
            span=heading,
            chunk_size_chars=chunk_size_chars,
        )
        _append_paragraph_pieces(
            candidates,
            document_revision_id=document_revision_id,
            block=block,
            span=body,
            chunk_size_chars=chunk_size_chars,
        )
        index = body_index + 1


def _append_paragraph_pieces(
    candidates: list[ChunkCandidate],
    *,
    document_revision_id: UUID,
    block: ParsedBlock,
    span: _Span,
    chunk_size_chars: int,
) -> None:
    span = _trim_span(block.text, span.start, span.end)
    if span.empty:
        return
    if _normalized_length(block.text, span) <= chunk_size_chars:
        _append_candidate(
            candidates,
            document_revision_id=document_revision_id,
            block=block,
            span=span,
            chunk_size_chars=chunk_size_chars,
        )
        return

    sentences = _sentence_spans(block.text, span)
    if len(sentences) <= 1:
        _append_bounded_pieces(
            candidates,
            document_revision_id=document_revision_id,
            block=block,
            span=span,
            chunk_size_chars=chunk_size_chars,
        )
        return

    group: _Span | None = None
    for sentence in sentences:
        if _normalized_length(block.text, sentence) > chunk_size_chars:
            _append_span_candidate(
                candidates,
                document_revision_id=document_revision_id,
                block=block,
                span=group,
                chunk_size_chars=chunk_size_chars,
            )
            group = None
            _append_bounded_pieces(
                candidates,
                document_revision_id=document_revision_id,
                block=block,
                span=sentence,
                chunk_size_chars=chunk_size_chars,
            )
            continue

        if group is None:
            group = sentence
            continue

        joined = _Span(group.start, sentence.end)
        if _normalized_length(block.text, joined) <= chunk_size_chars:
            group = joined
        else:
            _append_span_candidate(
                candidates,
                document_revision_id=document_revision_id,
                block=block,
                span=group,
                chunk_size_chars=chunk_size_chars,
            )
            group = sentence

    _append_span_candidate(
        candidates,
        document_revision_id=document_revision_id,
        block=block,
        span=group,
        chunk_size_chars=chunk_size_chars,
    )


def _append_bounded_pieces(
    candidates: list[ChunkCandidate],
    *,
    document_revision_id: UUID,
    block: ParsedBlock,
    span: _Span,
    chunk_size_chars: int,
) -> None:
    cursor = span.start
    end = span.end
    while cursor < end:
        remaining = _trim_span(block.text, cursor, end)
        cursor, end = remaining.start, remaining.end
        if remaining.empty:
            return
        if _normalized_length(block.text, _Span(cursor, end)) <= chunk_size_chars:
            piece_end = end
        else:
            safe_end = _largest_hard_prefix_end(block.text, cursor, end, chunk_size_chars)
            piece_end = _preferred_prefix_end(
                block.text,
                cursor,
                safe_end,
                minimum_content_start=cursor + 1,
            )
            if piece_end <= cursor:
                piece_end = min(cursor + 1, end)

        piece = _trim_span(block.text, cursor, piece_end)
        if piece.empty:
            cursor = max(piece_end, cursor + 1)
            continue
        _append_candidate(
            candidates,
            document_revision_id=document_revision_id,
            block=block,
            span=piece,
            chunk_size_chars=chunk_size_chars,
        )
        cursor = piece.end


def _append_span_candidate(
    candidates: list[ChunkCandidate],
    *,
    document_revision_id: UUID,
    block: ParsedBlock,
    span: _Span | None,
    chunk_size_chars: int,
) -> None:
    if span is None:
        return
    span = _trim_span(block.text, span.start, span.end)
    if span.empty:
        return
    _append_candidate(
        candidates,
        document_revision_id=document_revision_id,
        block=block,
        span=span,
        chunk_size_chars=chunk_size_chars,
    )


def _append_candidate(
    candidates: list[ChunkCandidate],
    *,
    document_revision_id: UUID,
    block: ParsedBlock,
    span: _Span,
    chunk_size_chars: int,
) -> None:
    span = _trim_span(block.text, span.start, span.end)
    if span.empty:
        return
    text = normalize_chunk_text(block.text[span.start : span.end])
    if not text:
        return
    if len(text) > chunk_size_chars:
        raise RuntimeError("structural splitter produced an oversized chunk")

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
            locator=_candidate_locator(block, span),
        )
    )


def _candidate_locator(block: ParsedBlock, span: _Span) -> dict[str, Any]:
    locator = dict(block.locator)
    existing_start = locator.get("char_start", 0)
    base = existing_start if type(existing_start) is int and existing_start >= 0 else 0
    locator["char_start"] = base + span.start
    locator["char_end"] = base + span.end
    return locator


def _paragraph_spans(text: str) -> tuple[_Span, ...]:
    spans: list[_Span] = []
    cursor = 0
    for separator in _PARAGRAPH_BREAK.finditer(text):
        span = _trim_span(text, cursor, separator.start())
        if not span.empty:
            spans.append(span)
        cursor = separator.end()
    span = _trim_span(text, cursor, len(text))
    if not span.empty:
        spans.append(span)
    return tuple(spans)


def _sentence_spans(text: str, span: _Span) -> tuple[_Span, ...]:
    boundaries = _sentence_boundaries(text, span)
    if not boundaries:
        return (span,)

    spans: list[_Span] = []
    cursor = span.start
    for boundary in boundaries:
        sentence = _trim_span(text, cursor, boundary)
        if not sentence.empty:
            spans.append(sentence)
        cursor = boundary
    tail = _trim_span(text, cursor, span.end)
    if not tail.empty:
        spans.append(tail)
    return tuple(spans)


def _sentence_boundaries(text: str, span: _Span) -> tuple[int, ...]:
    boundaries: list[int] = []
    index = span.start
    while index < span.end:
        character = text[index]
        if character in _CHINESE_SENTENCE_TERMINALS:
            boundary = index + 1
            while boundary < span.end and text[boundary] in _CLOSING_PUNCTUATION:
                boundary += 1
            boundaries.append(boundary)
            index = boundary
            continue

        if character in _ENGLISH_SENTENCE_TERMINALS:
            boundary = index + 1
            while boundary < span.end and text[boundary] in _ENGLISH_SENTENCE_TERMINALS:
                boundary += 1
            if boundary == span.end or text[boundary].isspace():
                while boundary < span.end and text[boundary] in _CLOSING_PUNCTUATION:
                    boundary += 1
                boundaries.append(boundary)
                index = boundary
                continue
        index += 1
    return tuple(boundaries)


def _largest_hard_prefix_end(text: str, start: int, end: int, limit: int) -> int:
    candidate_end = min(end, start + limit)
    while candidate_end > start and _normalized_length(text, _Span(start, candidate_end)) > limit:
        candidate_end -= 1
    return candidate_end


def _preferred_prefix_end(
    text: str,
    start: int,
    safe_end: int,
    *,
    minimum_content_start: int,
) -> int:
    sentence = [
        boundary
        for boundary in _sentence_boundaries(text, _Span(start, safe_end))
        if boundary >= minimum_content_start
    ]
    if sentence:
        return sentence[-1]

    whitespace_before = [
        match.start()
        for match in _WHITESPACE.finditer(text, start, safe_end)
        if match.start() >= minimum_content_start
    ]
    if whitespace_before:
        return whitespace_before[-1]
    return safe_end


def _normalized_length(text: str, span: _Span) -> int:
    return len(normalize_chunk_text(text[span.start : span.end]))


def _trim_span(text: str, start: int, end: int) -> _Span:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return _Span(start, end)


def _is_markdown_heading(text: str, span: _Span) -> bool:
    return _MARKDOWN_HEADING.match(text, span.start, span.end) is not None


__all__ = ["build_structural_chunks"]
