from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from uuid import UUID

import pytest

from apps.api.schemas.knowledge import KnowledgeBaseCreateRequest
from packages.core.config.settings import Settings
from packages.knowledge.adaptive_chunking import build_adaptive_chunks
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.chunking_policy import ChunkingPolicy, ChunkingStrategy
from packages.knowledge.parser import ParsedBlock, ParsedDocument

REVISION = UUID("00000000-0000-0000-0000-000000000057")


def _text(text: str, base: int = 0) -> ParsedDocument:
    return ParsedDocument((ParsedBlock(text, {"type": "text_range", "char_start": base}),))


def _check_slices(block, chunks):
    base = block.locator.get("char_start", 0)
    previous = 0
    for chunk in chunks:
        start, end = chunk.locator["char_start"] - base, chunk.locator["char_end"] - base
        assert previous <= start < end <= len(block.text)
        assert not block.text[previous:start].strip()
        assert chunk.text == normalize_chunk_text(block.text[start:end])
        previous = end
    assert not block.text[previous:].strip()


@pytest.mark.parametrize(
    ("media", "cap"),
    [
        ("text/plain", 1200),
        ("text/csv", 1200),
        ("text/tab-separated-values", 1200),
        ("text/markdown", 900),
        ("application/pdf", 800),
        ("application/json", 800),
    ],
)
def test_revision_resolves_caps_and_history_uses_recorded_values(media, cap):
    settings = Settings(knowledge_chunk_size_chars=1200, knowledge_chunk_overlap_chars=200)
    frozen = ChunkingPolicy.resolve(ChunkingStrategy.ADAPTIVE, media, settings)
    settings.knowledge_chunk_size_chars = 30
    assert frozen == ChunkingPolicy(ChunkingStrategy.ADAPTIVE, cap, 0)


def test_documented_legacy_strategies_reproduce_original_chunks_and_ids():
    assert KnowledgeBaseCreateRequest(name="KB").chunking_strategy == ChunkingStrategy.FIXED
    settings = Settings(knowledge_chunk_size_chars=52, knowledge_chunk_overlap_chars=9)
    parsed = _text("  # Start\n\nCafé policy.\n\n" + ("原句。参见附件！" * 25), base=5)
    # Full candidates (including text, ordinals, IDs and locators) produced by fafe4aba.
    fingerprints = {
        ChunkingStrategy.FIXED: "d440a66a4447eebfe509f1d5736e29e0f82a183154b2f22665378e977b647059",
        ChunkingStrategy.STRUCTURAL: (
            "94c77fd50e14bd969a21ce61d14ae047eb41858a962328491925ed9f34bee91d"
        ),
    }
    for strategy, expected in fingerprints.items():
        policy = ChunkingPolicy.resolve(strategy, "text/markdown", settings)
        chunks = policy.build(REVISION, parsed, media_type="text/markdown")
        encoded = json.dumps(
            [asdict(c) for c in chunks], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        assert hashlib.sha256(encoded).hexdigest() == expected


def test_markdown_sections_without_blank_lines_and_fences_keep_raw_coordinates():
    source = (
        "  preface \r\n# Refunds\r\nCafe\u0301 may settle.\r\n"
        "## Disputes\r\n争议单独处理。\r\n\r\n"
        "~~~markdown\r\n# example\r\nx | y\r\n:- | -:\r\n~~~\r\n"
        "Continue in disputes.\n# Closed\nEnd."
    )
    document = _text(source, 37)
    chunks = build_adaptive_chunks(
        REVISION, document, media_type="text/markdown", chunk_size_chars=1000
    )
    assert [chunk.text for chunk in chunks] == [
        "preface",
        "# Refunds\nCafé may settle.",
        "## Disputes\n争议单独处理。",
        "~~~markdown\n# example\nx | y\n:- | -:\n~~~",
        "Continue in disputes.",
        "# Closed\nEnd.",
    ]
    assert not any("table_index" in chunk.locator for chunk in chunks)
    assert chunks[4].locator["heading_char_start"] == 37 + source.index("## Disputes")
    _check_slices(document.blocks[0], chunks)


def test_markdown_table_packs_whole_rows_and_keeps_a_source_header_anchor():
    source = "# Rates\r\n| Name | Fee |\r\n| :--- | ---: |\r\n"
    rows = [f"| Plan {i} | {i * 5} EUR |" for i in range(1, 9)]
    document = _text(source + "\r\n".join(rows) + "\r\n\r\nFinal prose.", base=70)
    chunks = build_adaptive_chunks(
        REVISION, document, media_type="text/markdown", chunk_size_chars=48
    )
    tables = [c for c in chunks if "table_index" in c.locator]
    assert len(tables) > 1
    assert all(0 < len(c.text) <= 48 for c in chunks)
    for i, row in enumerate(rows, start=2):
        matched = [c for c in tables if row in c.text]
        assert len(matched) == 1
        assert matched[0].locator["row_start"] <= i <= matched[0].locator["row_end"]
        assert matched[0].locator["header_char_start"] == 70 + source.index("| Name")
        assert matched[0].locator["header_char_end"] == 70 + source.index(" |\r\n") + 2
    assert all("Final prose" not in c.text and "# Rates" not in c.text for c in tables)
    _check_slices(document.blocks[0], chunks)


def test_wide_parsed_row_splits_by_cells_and_retains_all_source_spans_and_geometry():
    values = ["North", "2026-11", "x" * 31, "42.6"]
    text = " | ".join(values)
    cells = []
    cursor = 0
    for number, value in enumerate(values, start=1):
        end = cursor + len(value)
        cells.append(
            {
                "column": number,
                "column_end": number,
                "row_end": 12,
                "char_start": cursor,
                "char_end": end,
            }
        )
        cursor = end + 3
    first = ParsedBlock(
        text,
        {
            "type": "table_row",
            "page": 3,
            "block_index": 4,
            "row": 12,
            "bbox": [1, 2, 3, 4],
            "cells": cells,
            "column_start": 1,
            "column_end": 4,
        },
    )
    second = replace(first, locator={**first.locator, "row": 13})
    doc = ParsedDocument((first, second))
    chunks = build_adaptive_chunks(
        REVISION, doc, media_type="application/json", chunk_size_chars=16
    )
    assert chunks == build_adaptive_chunks(
        REVISION, doc, media_type="application/json", chunk_size_chars=16
    )
    assert len({c.chunk_id for c in chunks}) == len(chunks)
    for row in (12, 13):
        group = [c for c in chunks if c.locator["row"] == row]
        _check_slices(first, group)
        assert all(
            c.locator["bbox"] == [1, 2, 3, 4] and c.locator["block_index"] == 4 for c in group
        )
        assert all(len(c.text) <= 16 for c in group)
        for c in group:
            intersect = [
                cell
                for cell in cells
                if cell["char_start"] < c.locator["char_end"]
                and c.locator["char_start"] < cell["char_end"]
            ]
            assert c.locator["cells"] == intersect
            if intersect:
                assert c.locator["column_start"] == intersect[0]["column"]
                assert c.locator["column_end"] == intersect[-1]["column_end"]
    assert cells == first.locator["cells"] and first.locator["column_start"] == 1


def test_page_and_plain_paragraphs_do_not_cross_source_boundaries():
    blocks = tuple(
        ParsedBlock("第一段。\r\n\r\n第二段。", {"type": "page", "page": i}) for i in (3, 4)
    )
    chunks = build_adaptive_chunks(
        REVISION, ParsedDocument(blocks), media_type="application/pdf", chunk_size_chars=800
    )
    assert [c.locator["page"] for c in chunks] == [3, 3, 4, 4]
    for block in blocks:
        _check_slices(block, [c for c in chunks if c.locator["page"] == block.locator["page"]])
    plain = _text("# heading\nliteral hash\n\nfurther details")
    chunks = build_adaptive_chunks(REVISION, plain, media_type="text/plain", chunk_size_chars=1200)
    assert [c.text for c in chunks] == ["# heading\nliteral hash", "further details"]
    assert all("heading_char_start" not in c.locator for c in chunks)


@pytest.mark.parametrize("strategy", list(ChunkingStrategy))
def test_layout_cells_are_clipped_against_raw_crlf_and_nfc_offsets_in_every_strategy(strategy):
    text = "Cafe\u0301\r\norigin | 金额=54 | South"
    spans = [
        (0, text.index(" |")),
        (text.index("金额"), text.index(" | South")),
        (text.index("South"), len(text)),
    ]
    cells = [
        {"column": i, "column_end": i, "char_start": start, "char_end": end}
        for i, (start, end) in enumerate(spans, start=1)
    ]
    row = ParsedBlock(
        text, {"type": "table_row", "row": 1, "column_start": 1, "column_end": 3, "cells": cells}
    )
    config = Settings(knowledge_chunk_size_chars=11, knowledge_chunk_overlap_chars=2)
    chunks = ChunkingPolicy.resolve(strategy, "text/csv", config).build(
        REVISION, ParsedDocument((row,)), media_type="text/csv"
    )
    for c in chunks:
        begin, end = c.locator["char_start"], c.locator["char_end"]
        assert c.text == normalize_chunk_text(text[begin:end])
        assert c.locator["cells"] == [
            cell for cell in cells if cell["char_start"] < end and begin < cell["char_end"]
        ]
        if c.locator["cells"]:
            assert c.locator["column_start"] == c.locator["cells"][0]["column"]
            assert c.locator["column_end"] == c.locator["cells"][-1]["column_end"]


def test_setext_and_layout_headings_anchor_their_respective_source_blocks():
    markdown = _text(
        "Intro\n\nRates\n=====\nThe monthly charge is five.\n\nDisputes\n--------\nAsk for review."
    )
    chunks = build_adaptive_chunks(
        REVISION, markdown, media_type="text/markdown", chunk_size_chars=100
    )
    assert [c.locator.get("heading_char_start") for c in chunks] == [
        None,
        markdown.blocks[0].text.index("Rates"),
        markdown.blocks[0].text.index("Disputes"),
    ]
    layout = ParsedDocument(
        tuple(
            ParsedBlock(text, {"type": "page", "page": 1, "block_index": i, "block_kind": kind})
            for i, (kind, text) in enumerate(
                [("heading", "Rates"), ("text", "Five"), ("heading", "Hours"), ("text", "Nine")]
            )
        )
    )
    chunks = build_adaptive_chunks(
        REVISION, layout, media_type="application/json", chunk_size_chars=100
    )
    assert [c.locator["heading_block_index"] for c in chunks] == [0, 0, 2, 2]
    assert [c.locator["block_index"] for c in chunks] == [0, 1, 2, 3]
