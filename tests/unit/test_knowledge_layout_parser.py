from __future__ import annotations

import json

import pytest

from packages.knowledge.evidence_plan import model_evidence_locator
from packages.knowledge.evidence_records import drop_overlapping_records
from packages.knowledge.parser import DocumentParseError, ProcessDocumentParser, _parse_content


def _parse(content: bytes, media: str = "application/json", pages: int = 10, chars: int = 8192):
    return _parse_content(content, media, max_pdf_pages=pages, max_parsed_chars=chars)


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode()


def _cell_values(block) -> list[str]:
    return [block.text[cell["value_start"] : cell["char_end"]] for cell in block.locator["cells"]]


def test_csv_and_tsv_keep_logical_cells_headers_multiline_and_physical_lines() -> None:
    csv = '\ufeff地区,说明,价格\r\n西部,"A, B\r\n含""引号""",42\r\n东部,,8\r\n'
    header, west, east = _parse(csv.encode(), "text/csv").blocks
    assert _cell_values(header) == ["地区", "说明", "价格"]
    assert _cell_values(west) == ["西部", 'A, B\r\n含"引号"', "42"]
    assert "价格: 42" in west.text
    assert (west.locator["row"], west.locator["line_start"], west.locator["line_end"]) == (2, 2, 3)
    assert _cell_values(east) == ["东部", "", "8"]
    assert (east.locator["row"], east.locator["line_start"]) == (3, 4)
    assert [cell["column"] for cell in east.locator["cells"]] == [1, 2, 3]

    tsv = _parse(b'key\tvalue\nwest\t"x\ty"\n', "text/tab-separated-values")
    assert [_cell_values(row) for row in tsv.blocks] == [["key", "value"], ["west", "x\ty"]]


def test_mineru_flat_and_file_parse_envelope_keep_order_geometry_and_merged_cells() -> None:
    items = [
        {"page_idx": 0, "type": "text", "text": "Summary", "text_level": 1},
        {
            "page_idx": 2,
            "type": "image",
            "img_path": "https://example.invalid/scan",
            "image_caption": ["Scan A"],
            "bbox": [1, 2, 900, 999],
        },
        {
            "page_idx": 2,
            "type": "table",
            "table_caption": ["Quarterly revenue"],
            "table_footnote": ["Values in USD"],
            "table_body": (
                '<html><body><table><thead><tr><th rowspan="2">Region</th>'
                '<th colspan="2">Revenue</th></tr><tr><th>Q1</th><th>Q2</th></tr></thead>'
                '<tbody><tr><td rowspan="2">West</td><td>10</td><td>12</td></tr>'
                "<tr><td>A&amp;B<br>Ltd</td><td>21<script>hidden()</script></td></tr>"
                "</tbody></table></body></html>"
            ),
            "bbox": [10, 20, 400, 600],
        },
        {"page_idx": 2, "type": "list", "list_items": ["One", "Two"]},
        {"page_idx": 2, "type": "code", "code_body": "print(2)"},
        {"page_idx": 2, "type": "equation", "text": "x = 2"},
    ]
    parsed = _parse(_json(items))
    response = {
        "backend": "pipeline",
        "version": "3.3.1",
        "results": {
            "report": {
                "content_list": json.dumps(items),
                "md_content": "discarded representation",
            }
        },
    }
    assert _parse(_json(response)) == parsed
    title, image, caption, header1, header2, west, more, footnote, listing, code, eq = parsed.blocks
    assert title.locator == {
        "type": "page",
        "page": 1,
        "block_index": 0,
        "block_kind": "heading",
        "heading_level": 1,
    }
    assert image.text == "Scan A"
    assert caption.text == "Quarterly revenue" and footnote.text == "Values in USD"
    assert (caption.locator["part_index"], footnote.locator["part_index"]) == (0, 2)
    assert _cell_values(header1) == ["Region", "Revenue"]
    assert header1.locator["cells"][0]["row_end"] == 2
    assert header1.locator["cells"][1]["column_end"] == 3
    assert _cell_values(header2) == ["Region", "Q1", "Q2"]
    assert _cell_values(west) == ["West", "10", "12"]
    assert more.text == "Region: West | Q1: A&B Ltd | Q2: 21"
    assert more.locator["cells"][0]["row"] == 3
    assert (more.locator["page"], more.locator["block_index"], more.locator["row"]) == (3, 1, 4)
    assert more.locator["bbox"] == [10, 20, 400, 600]
    assert [listing.text, code.text, eq.text] == ["One\nTwo", "print(2)", "x = 2"]
    assert [
        listing.locator["block_index"],
        code.locator["block_index"],
        eq.locator["block_index"],
    ] == [2, 3, 4]
    assert "example.invalid" not in str(parsed.blocks)


@pytest.mark.parametrize(
    ("content", "media", "pages", "chars", "error"),
    [
        (b'[[{"type":"paragraph"}]]', "application/json", 10, 100, "PARSE_FAILED"),
        (
            _json({"data": {"full_zip_url": "https://example.invalid/zip"}}),
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            _json({"results": {"a": {"content_list": []}, "b": {"content_list": []}}}),
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            b'[{"type":"text","text":"a","text":"b","page_idx":0}]',
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            b'[{"type":"text","text":"\\u0000","page_idx":0}]',
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            b'[{"type":"text","text":"\\ud800","page_idx":0}]',
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            _json([{"page_idx": False, "type": "text", "text": "x"}]),
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            _json([{"page_idx": 1, "type": "text", "text": "x", "bbox": [7, 2, 1, 2]}]),
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            _json([{"page_idx": 3, "type": "text", "text": "x"}]),
            "application/json",
            3,
            100,
            "RESOURCE_LIMIT_EXCEEDED",
        ),
        (
            _json(
                [
                    {
                        "page_idx": 0,
                        "type": "table",
                        "table_body": '<table><tr><td colspan="999999">X</td></tr></table>',
                    }
                ]
            ),
            "application/json",
            10,
            100,
            "RESOURCE_LIMIT_EXCEEDED",
        ),
        (
            _json(
                [
                    {
                        "page_idx": 0,
                        "type": "table",
                        "table_body": '<table><tr><td rowspan="2">X</td></tr></table>',
                    }
                ]
            ),
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (
            _json([{"page_idx": 0, "type": "image", "img_path": "../raw"}]),
            "application/json",
            10,
            100,
            "PARSE_FAILED",
        ),
        (b'a,b\nx,"no ending', "text/csv", 10, 100, "PARSE_FAILED"),
        (b"a,b\nx,\xff", "text/csv", 10, 100, "PARSE_FAILED"),
        (b"first\n1\n2", "text/csv", 10, 10, "RESOURCE_LIMIT_EXCEEDED"),
        (b"," * 256, "text/csv", 10, 100, "RESOURCE_LIMIT_EXCEEDED"),
    ],
)
def test_structured_parser_rejects_ambiguous_or_excessive_inputs(
    content: bytes, media: str, pages: int, chars: int, error: str
) -> None:
    with pytest.raises(DocumentParseError) as exc:
        _parse(content, media, pages, chars)
    assert exc.value.code == error


@pytest.mark.asyncio
async def test_mineru_ocr_blocks_survive_isolated_parser_and_evidence_projection() -> None:
    body = [
        {"type": "text", "text": f"recognized {index}", "page_idx": 0, "bbox": [0, 0, 40, 50]}
        for index in (1, 2)
    ]
    parser = ProcessDocumentParser(timeout_seconds=10, max_pdf_pages=2, max_parsed_chars=120)
    parsed = await parser.parse(_json(body), media_type="application/json")
    assert [block.text for block in parsed.blocks] == ["recognized 1", "recognized 2"]
    records = [
        {
            "document_revision_id": "revision",
            "chunk_id": str(index),
            "locator": {**block.locator, "char_start": 0, "char_end": len(block.text)},
        }
        for index, block in enumerate(parsed.blocks)
    ]
    rows = _parse(b"a,b\n1,2\n3,4", "text/csv")
    records.extend(
        [
            {
                "document_revision_id": "table",
                "chunk_id": str(index),
                "locator": {**block.locator, "char_start": 0, "char_end": len(block.text)},
            }
            for index, block in enumerate(rows.blocks)
        ]
    )
    kept, _ = drop_overlapping_records([*records, records[-1]], [*range(5), 4])
    assert kept == records
    assert model_evidence_locator(records[-1]["locator"]) == {
        "type": "table_row",
        "row": 3,
        "column_start": 1,
        "column_end": 2,
        "char_start": 0,
        "char_end": 11,
    }
