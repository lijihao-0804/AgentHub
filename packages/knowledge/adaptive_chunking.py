"""Split source blocks without synthesizing text or joining different locator spaces."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import replace
from uuid import UUID

from packages.knowledge.chunking import ChunkCandidate, normalize_chunk_text
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.structural_chunking import (
    _MARKDOWN_HEADING,
    _append_block_candidates,
    _append_paragraph_pieces,
    _candidate_locator,
    _paragraph_spans,
    _Span,
)

_LINE = re.compile(r"[^\r\n]*(?:\r\n|[\r\n]|$)")
_ALIGNMENT_CELL = re.compile(r":?-+:?")
_SETEXT = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
_FENCE = re.compile(r" {0,3}(\x60{3,}|~{3,})")
_PIPE = re.compile(r"(?<!\\)\|")


def _line(text: str, start: int) -> re.Match[str]:
    # Called only at a source line boundary strictly before EOF.
    match = _LINE.match(text, start)
    assert match is not None
    return match


def _table_delimiter(header: str, line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return (
        "|" in line
        and len(cells) == len(_PIPE.split(header.strip().strip("|")))
        and all(_ALIGNMENT_CELL.fullmatch(cell.strip()) for cell in cells)
    )


def _markdown_regions(text: str) -> Iterator[tuple[str, _Span, _Span | None]]:
    """Find sections, fenced blocks and pipe tables directly in raw character space."""
    section_start = cursor = 0
    heading = None
    while cursor < len(text):
        line = _line(text, cursor)
        content = line.group().rstrip("\r\n")
        end = line.end()
        following = _line(text, end) if end < len(text) else None
        setext = bool(
            content.strip()
            and not _FENCE.match(content)
            and following
            and _SETEXT.fullmatch(following.group().rstrip("\r\n"))
        )
        if _MARKDOWN_HEADING.match(content) or setext:
            if section_start < cursor:
                yield "section", _Span(section_start, cursor), heading
            section_start = cursor
            heading = _Span(cursor, cursor + len(content))
            if setext and following:
                end = following.end()
        elif fence := _FENCE.match(content):
            if section_start < cursor:
                yield "section", _Span(section_start, cursor), heading
            marker = fence.group(1)
            while end < len(text):
                next_line = _line(text, end)
                end = next_line.end()
                closer = next_line.group().strip()
                if len(closer) >= len(marker) and closer.strip(marker[0]) == "":
                    break
            yield "lines", _Span(cursor, end), heading
            section_start = end
        elif "|" in content and following and _table_delimiter(content, following.group()):
            if section_start < cursor:
                yield "section", _Span(section_start, cursor), heading
            end = following.end()
            while end < len(text):
                row = _line(text, end)
                if "|" not in row.group() or _MARKDOWN_HEADING.match(row.group()):
                    break
                end = row.end()
            yield "table", _Span(cursor, end), heading
            section_start = end
        cursor = end
    if section_start < len(text):
        yield "section", _Span(section_start, len(text)), heading


def _pack_units(text: str, units: Iterable[_Span], cap: int) -> Iterator[tuple[int, int, _Span]]:
    first = last = 0
    group = None
    for number, unit in enumerate(units, start=1):
        if group is not None and len(normalize_chunk_text(text[group.start : unit.end])) > cap:
            yield first, last, group
            group = None
        if group is None:
            group = unit
            first = number
        else:
            group = _Span(group.start, unit.end)
        last = number
    if group is not None:
        yield first, last, group


def _append_piece(
    out: list[ChunkCandidate], revision: UUID, block: ParsedBlock, span: _Span, cap: int
) -> None:
    _append_paragraph_pieces(
        out, document_revision_id=revision, block=block, span=span, chunk_size_chars=cap
    )


def _cell_spans(span: _Span, boundaries: Iterable[int]) -> Iterator[_Span]:
    start = span.start
    for boundary in boundaries:
        if start < boundary < span.end:
            yield _Span(start, boundary)
            start = boundary
    if start < span.end:
        yield _Span(start, span.end)


def _append_parsed_row(
    out: list[ChunkCandidate], revision: UUID, block: ParsedBlock, cap: int
) -> None:
    cells = block.locator.get("cells", ())
    span = _Span(0, len(block.text))
    if len(normalize_chunk_text(block.text)) <= cap or not cells:
        _append_piece(out, revision, block, span, cap)
        return

    # Locator cells are expressed in block coordinates. Keep a column intact
    # when it fits, and retain only the source cells intersecting each excerpt.
    units = _cell_spans(span, (cell["char_start"] for cell in cells))
    for _, _, group in _pack_units(block.text, units, cap):
        _append_piece(out, revision, block, group, cap)


def _line_units(block: ParsedBlock, span: _Span, *, table: bool) -> Iterator[_Span]:
    start = span.start
    if table:
        # The alignment delimiter belongs to the original header, not a data row.
        end = _line(block.text, _line(block.text, start).end()).end()
        yield _Span(start, end)
        start = end
    while start < span.end:
        end = _line(block.text, start).end()
        yield _Span(start, end)
        start = end


def _append_markdown(
    out: list[ChunkCandidate], revision: UUID, block: ParsedBlock, cap: int
) -> None:
    table_index = 0
    for kind, span, heading in _markdown_regions(block.text):
        locator = dict(block.locator)
        if heading:
            heading_anchor = _candidate_locator(block, heading)
            locator.update(
                heading_char_start=heading_anchor["char_start"],
                heading_char_end=heading_anchor["char_end"],
            )
        region = replace(block, locator=locator)
        if kind == "section":
            local = ParsedBlock(block.text[span.start : span.end], _candidate_locator(region, span))
            if len(local.text.strip()) <= cap:
                _append_piece(out, revision, local, _Span(0, len(local.text)), cap)
            else:
                _append_block_candidates(
                    out, document_revision_id=revision, block=local, chunk_size_chars=cap
                )
            continue

        table = kind == "table"
        if table:
            table_index += 1
            header = _line(block.text, span.start).group().rstrip("\r\n")
            header_anchor = _candidate_locator(region, _Span(span.start, span.start + len(header)))
            locator.update(
                table_index=table_index,
                header_char_start=header_anchor["char_start"],
                header_char_end=header_anchor["char_end"],
            )
        for first, last, group in _pack_units(
            block.text, _line_units(block, span, table=table), cap
        ):
            if table:
                row_block = replace(
                    region, locator={**locator, "row_start": first, "row_end": last}
                )
                if (
                    first == last
                    and len(normalize_chunk_text(block.text[group.start : group.end])) > cap
                ):
                    pipes = (
                        match.end() for match in _PIPE.finditer(block.text, group.start, group.end)
                    )
                    for _, _, part in _pack_units(block.text, _cell_spans(group, pipes), cap):
                        _append_piece(out, revision, row_block, part, cap)
                else:
                    _append_piece(out, revision, row_block, group, cap)
            else:
                _append_piece(out, revision, region, group, cap)


def build_adaptive_chunks(
    revision_id: UUID, parsed: ParsedDocument, *, media_type: str, chunk_size_chars: int
) -> tuple[ChunkCandidate, ...]:
    """Use a revision's resolved cap. Every candidate cites one contiguous parsed span."""
    if type(chunk_size_chars) is not int or chunk_size_chars <= 0:
        raise ValueError("chunk_size_chars must be a positive integer")
    out: list[ChunkCandidate] = []
    heading = {}
    for block in parsed.blocks:
        if media_type == "application/json" and "block_index" in block.locator:
            if block.locator.get("block_kind") == "heading":
                heading = {
                    "heading_page": block.locator["page"],
                    "heading_block_index": block.locator["block_index"],
                }
            if heading:
                block = replace(block, locator={**block.locator, **heading})
        if block.locator.get("type") == "table_row":
            _append_parsed_row(out, revision_id, block, chunk_size_chars)
        elif media_type == "text/markdown":
            _append_markdown(out, revision_id, block, chunk_size_chars)
        else:
            for paragraph in _paragraph_spans(block.text):
                _append_piece(out, revision_id, block, paragraph, chunk_size_chars)
    return tuple(out)
