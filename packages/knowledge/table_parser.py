"""Read delimited and MinerU HTML tables as independently citable rows."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from packages.knowledge.parser_types import (
    MAX_STRUCTURED_BLOCKS,
    MAX_TABLE_CELLS,
    DocumentParseError,
    ParsedBlock,
    structure_limit,
    text_limit,
)

_MAX_COLUMNS = 256
_SEPARATOR = " | "


@dataclass(frozen=True)
class _Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False


@dataclass(frozen=True)
class _PositionedCell:
    cell: _Cell
    row: int
    column: int

    @property
    def column_end(self) -> int:
        return self.column + self.cell.colspan - 1

    @property
    def row_end(self) -> int:
        return self.row + self.cell.rowspan - 1


def _row_block(
    cells: list[_PositionedCell],
    headers: dict[int, str],
    *,
    is_header: bool,
    locator: dict[str, Any],
    max_chars: int,
) -> ParsedBlock:
    fields: list[str] = []
    positions = []
    end = 0
    for cell in cells:
        value = cell.cell.text
        # A value spanning different column headers has no unambiguous single label.
        label = "" if is_header else headers.get(cell.column, "")
        if label and any(
            headers.get(col) != label for col in range(cell.column, cell.column_end + 1)
        ):
            label = ""
        start = end + (len(_SEPARATOR) if fields else 0)
        value_start = start + (len(label) + 2 if label else 0)
        end = value_start + len(value)
        if end > max_chars:
            raise text_limit()
        fields.append(f"{label}: {value}" if label else value)
        positions.append(
            {
                "row": cell.row,
                "row_end": cell.row_end,
                "column": cell.column,
                "column_end": cell.column_end,
                "char_start": start,
                "value_start": value_start,
                "char_end": end,
            }
        )
    return ParsedBlock(
        _SEPARATOR.join(fields),
        {
            **locator,
            "type": "table_row",
            "column_start": cells[0].column,
            "column_end": cells[-1].column_end,
            "cells": positions,
        },
    )


def _table_blocks(
    rows: Iterable[tuple[list[_Cell], dict[str, int]]],
    *,
    locator: dict[str, Any],
    max_chars: int,
) -> Iterator[ParsedBlock]:
    headers: dict[int, str] = {}
    carried: list[_PositionedCell] = []
    cell_count = 0
    last_row = 0
    for last_row, (source_cells, row_locator) in enumerate(rows, start=1):
        if last_row > MAX_STRUCTURED_BLOCKS or len(source_cells) > _MAX_COLUMNS:
            raise structure_limit()
        positioned = [cell for cell in carried if cell.row_end >= last_row]
        occupied = {col for cell in positioned for col in range(cell.column, cell.column_end + 1)}
        column = 1
        for source in source_cells:
            while column in occupied:
                column += 1
            cell = _PositionedCell(source, last_row, column)
            if cell.column_end > _MAX_COLUMNS or cell.row_end > MAX_STRUCTURED_BLOCKS:
                raise structure_limit()
            if any(col in occupied for col in range(column, cell.column_end + 1)):
                raise DocumentParseError("PARSE_FAILED", "The table has overlapping merged cells.")
            positioned.append(cell)
            column = cell.column_end + 1
        carried = [cell for cell in positioned if cell.row_end > last_row]
        cell_count += len(positioned)
        if cell_count > MAX_TABLE_CELLS:
            raise structure_limit()
        if not any(cell.cell.text.strip() for cell in positioned):
            continue
        positioned.sort(key=lambda cell: cell.column)
        is_header = bool(positioned) and all(cell.cell.is_header for cell in positioned)
        yield _row_block(
            positioned,
            headers,
            is_header=is_header,
            locator={**locator, **row_locator, "row": last_row},
            max_chars=max_chars,
        )
        if is_header:
            for cell in positioned:
                for col in range(cell.column, cell.column_end + 1):
                    headers[col] = cell.cell.text
    if carried:
        raise DocumentParseError("PARSE_FAILED", "A merged cell extends past the table.")


def _delimited_rows(text: str, delimiter: str, max_chars: int):
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    first_record = True
    line_start = 1
    # This generator is consumed inside the isolated parser process. Restore the
    # limit as well for callers exercising the parser synchronously.
    previous = csv.field_size_limit(max_chars)
    try:
        for record in reader:
            if len(record) > _MAX_COLUMNS:
                raise structure_limit()
            cells = [_Cell(value, is_header=first_record) for value in record]
            if any(value.strip() for value in record):
                first_record = False
            yield cells, {"line_start": line_start, "line_end": reader.line_num}
            line_start = reader.line_num + 1
    except csv.Error as exc:
        if "field larger" in str(exc):
            raise text_limit() from exc
        raise DocumentParseError("PARSE_FAILED", "The delimited table is malformed.") from exc
    finally:
        csv.field_size_limit(previous)


def delimited_blocks(text: str, *, delimiter: str, max_chars: int) -> Iterator[ParsedBlock]:
    return _table_blocks(
        _delimited_rows(text, delimiter, max_chars), locator={}, max_chars=max_chars
    )


def _bad_html() -> DocumentParseError:
    return DocumentParseError("PARSE_FAILED", "The MinerU table HTML is malformed or unsupported.")


def _span(value: str | None, maximum: int) -> int:
    if value is None:
        return 1
    value = value.strip()
    if not value.isascii() or not value.isdecimal() or not value.strip("0"):
        raise _bad_html()
    if len(value) > len(str(maximum)) or int(value) > maximum:
        raise structure_limit()
    return int(value)


class _HTMLTable(HTMLParser):
    """Read the single table_body element. Links and images are never resolved."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self.in_table = False
        self.finished = False
        self.in_header = False
        self.row: list[_Cell] | None = None
        self.fragments: list[str] | None = None
        self.rowspan = self.colspan = 1
        self.is_header = False
        self.cell_count = 0
        self.ignored_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.ignored_tag:
            return
        if tag in {"script", "style"}:
            self.ignored_tag = tag
        elif tag == "table":
            if self.in_table or self.finished:
                raise _bad_html()
            self.in_table = True
        elif self.in_table and tag == "thead":
            self.in_header = True
        elif self.in_table and tag == "tr":
            self._end_row()
            if len(self.rows) >= MAX_STRUCTURED_BLOCKS:
                raise structure_limit()
            self.row = []
        elif self.in_table and tag in {"td", "th"}:
            if self.row is None:
                raise _bad_html()
            self._end_cell()
            self.cell_count += 1
            if self.cell_count > MAX_TABLE_CELLS or len(self.row) >= _MAX_COLUMNS:
                raise structure_limit()
            attributes = dict(attrs)
            self.rowspan = _span(attributes.get("rowspan"), MAX_STRUCTURED_BLOCKS)
            self.colspan = _span(attributes.get("colspan"), _MAX_COLUMNS)
            self.is_header = self.in_header or (
                tag == "th" and attributes.get("scope") not in {"row", "rowgroup"}
            )
            self.fragments = []
        elif self.fragments is not None and tag in {"br", "p", "div"}:
            self.fragments.append(" ")

    def handle_data(self, data: str) -> None:
        if self.fragments is not None and self.ignored_tag is None:
            self.fragments.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_tag:
            if self.ignored_tag == tag:
                self.ignored_tag = None
            return
        if tag in {"td", "th"}:
            self._end_cell()
        elif tag == "tr":
            self._end_row()
        elif tag == "thead":
            self.in_header = False
        elif tag == "table":
            if not self.in_table:
                raise _bad_html()
            self._end_row()
            self.in_table = False
            self.finished = True

    def _end_cell(self) -> None:
        if self.fragments is not None and self.row is not None:
            value = " ".join("".join(self.fragments).split())
            self.row.append(_Cell(value, self.rowspan, self.colspan, self.is_header))
            self.fragments = None

    def _end_row(self) -> None:
        self._end_cell()
        if self.row is not None:
            self.rows.append(self.row)
            self.row = None


def html_table_blocks(html: str, locator: dict[str, Any], max_chars: int) -> Iterator[ParsedBlock]:
    parser = _HTMLTable()
    parser.feed(html)
    parser.close()
    if not parser.finished or not parser.cell_count:
        raise _bad_html()
    return _table_blocks(
        ((cells, {}) for cells in parser.rows), locator=locator, max_chars=max_chars
    )
