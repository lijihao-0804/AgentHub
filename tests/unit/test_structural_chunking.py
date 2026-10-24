from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

import pytest

from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.structural_chunking import build_structural_chunks

REVISION_ID = UUID("00000000-0000-0000-0000-000000000101")


def _document(text: str, *, char_start: int = 0) -> ParsedDocument:
    return ParsedDocument(
        blocks=(
            ParsedBlock(
                text=text,
                locator={
                    "type": "text_range",
                    "char_start": char_start,
                    "char_end": char_start + len(text),
                },
            ),
        )
    )


def _non_whitespace(value: str) -> str:
    return "".join(value.split())


def test_preserves_paragraphs_without_filling_cap_from_neighbors() -> None:
    text = "First paragraph.\n\nSecond paragraph."

    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text),
        chunk_size_chars=1_000,
    )

    assert [chunk.text for chunk in chunks] == [
        "First paragraph.",
        "Second paragraph.",
    ]


def test_splits_oversized_paragraph_at_sentence_boundaries() -> None:
    text = (
        "First sentence is concise. "
        "Second sentence stays complete. "
        "Third sentence closes the paragraph."
    )

    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text),
        chunk_size_chars=42,
    )

    assert len(chunks) >= 2
    assert all(chunk.text.endswith(".") for chunk in chunks)
    assert _non_whitespace("".join(chunk.text for chunk in chunks)) == _non_whitespace(text)
    assert all(len(chunk.text) <= 42 for chunk in chunks)


def test_oversized_sentence_prefers_whitespace_then_character_fallback() -> None:
    prose = "alpha beta gamma delta epsilon"
    unbroken = "x" * 11

    prose_chunks = build_structural_chunks(
        REVISION_ID,
        _document(prose),
        chunk_size_chars=18,
    )
    unbroken_chunks = build_structural_chunks(
        REVISION_ID,
        _document(unbroken),
        chunk_size_chars=4,
    )

    for piece in prose_chunks:
        start = piece.locator["char_start"]
        end = piece.locator["char_end"]
        assert piece.text == prose[start:end]
        assert start == 0 or prose[start - 1].isspace()
    assert [chunk.text for chunk in unbroken_chunks] == ["xxxx", "xxxx", "xxx"]


def test_attaches_markdown_heading_to_following_paragraph_when_it_fits() -> None:
    text = "# Retrieval context\n\nUse the retrieved passage only."

    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text),
        chunk_size_chars=200,
    )

    assert [chunk.text for chunk in chunks] == [text]


def test_recognizes_heading_after_an_earlier_paragraph() -> None:
    text = "Preface.\n\n# Retrieval context\n\nUse the retrieved passage only."

    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text),
        chunk_size_chars=200,
    )

    assert [chunk.text for chunk in chunks] == [
        "Preface.",
        "# Retrieval context\n\nUse the retrieved passage only.",
    ]


def test_attaches_heading_to_first_bounded_body_part_when_full_body_is_too_long() -> None:
    text = (
        "# Overview\n\n"
        "This leading body sentence does not fit with its heading. "
        "The remaining sentence continues separately."
    )

    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text),
        chunk_size_chars=32,
    )

    assert chunks[0].text.startswith("# Overview\n\nThis")
    assert _non_whitespace("".join(chunk.text for chunk in chunks)) == _non_whitespace(text)
    assert all(len(chunk.text) <= 32 for chunk in chunks)


def test_returns_exact_normalized_slices_for_nonzero_block_offsets() -> None:
    first_text = "  Alpha paragraph.\r\n\r\nBeta paragraph.  "
    second_text = "Gamma paragraph."
    document = ParsedDocument(
        blocks=(
            ParsedBlock(
                text=first_text,
                locator={
                    "type": "text_range",
                    "char_start": 50,
                    "char_end": 50 + len(first_text),
                },
            ),
            ParsedBlock(
                text=second_text,
                locator={
                    "type": "text_range",
                    "char_start": 100,
                    "char_end": 100 + len(second_text),
                },
            ),
        )
    )

    chunks = build_structural_chunks(
        REVISION_ID,
        document,
        chunk_size_chars=24,
    )

    for chunk in chunks:
        block = next(
            item
            for item in document.blocks
            if item.locator["char_start"] <= chunk.locator["char_start"] < item.locator["char_end"]
        )
        local_start = chunk.locator["char_start"] - block.locator["char_start"]
        local_end = chunk.locator["char_end"] - block.locator["char_start"]
        assert chunk.text == normalize_chunk_text(block.text[local_start:local_end])


def test_keeps_pdf_pages_separate_and_preserves_page_locator() -> None:
    document = ParsedDocument(
        blocks=(
            ParsedBlock(text="Page one.", locator={"type": "page", "page": 1}),
            ParsedBlock(text="Page two.", locator={"type": "page", "page": 2}),
        )
    )

    chunks = build_structural_chunks(
        REVISION_ID,
        document,
        chunk_size_chars=1_000,
    )

    assert [chunk.text for chunk in chunks] == ["Page one.", "Page two."]
    assert [(chunk.locator["page"], chunk.locator["char_start"]) for chunk in chunks] == [
        (1, 0),
        (2, 0),
    ]


def test_normalizes_crlf_and_combining_marks_without_changing_raw_coordinates() -> None:
    text = "Cafe\u0301\r\n\r\nnai\u0308ve te\u0301xt  "
    document = _document(text, char_start=17)

    chunks = build_structural_chunks(
        REVISION_ID,
        document,
        chunk_size_chars=1_000,
    )

    assert [chunk.text for chunk in chunks] == ["Café", "naïve téxt"]
    for chunk in chunks:
        local_start = chunk.locator["char_start"] - 17
        local_end = chunk.locator["char_end"] - 17
        assert chunk.locator["char_end"] > chunk.locator["char_start"]
        assert chunk.text == normalize_chunk_text(text[local_start:local_end])


@pytest.mark.parametrize("chunk_size_chars", [1, 2, 3, 7])
def test_small_caps_reach_tail_without_dropping_non_whitespace(
    chunk_size_chars: int,
) -> None:
    text = "甲乙丙丁戊己庚辛壬癸"

    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text),
        chunk_size_chars=chunk_size_chars,
    )

    assert "".join(chunk.text for chunk in chunks) == text
    assert all(0 < len(chunk.text) <= chunk_size_chars for chunk in chunks)


@pytest.mark.parametrize(
    "invalid_cap",
    [True, False, 0, -1, 1.5, "10"],
)
def test_rejects_invalid_chunk_size(invalid_cap: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        build_structural_chunks(
            REVISION_ID,
            _document("content"),
            chunk_size_chars=invalid_cap,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("unsupported_overlap", [1, 2, -1, True])
def test_rejects_nonzero_overlap(unsupported_overlap: object) -> None:
    with pytest.raises(ValueError, match="overlap is unsupported"):
        build_structural_chunks(
            REVISION_ID,
            _document("content"),
            chunk_size_chars=10,
            chunk_overlap_chars=unsupported_overlap,  # type: ignore[arg-type]
        )


def test_empty_and_whitespace_only_blocks_produce_no_candidates() -> None:
    document = ParsedDocument(
        blocks=(
            ParsedBlock(text="", locator={"type": "text_range"}),
            ParsedBlock(text=" \r\n\t ", locator={"type": "text_range"}),
        )
    )

    assert build_structural_chunks(REVISION_ID, document) == ()


def test_locator_spans_are_contiguous_source_slices() -> None:
    text = "First paragraph.\n\nSecond paragraph."
    chunks = build_structural_chunks(
        REVISION_ID,
        _document(text, char_start=1_000),
        chunk_size_chars=1_000,
    )

    ranges: Iterable[tuple[int, int]] = (
        (chunk.locator["char_start"], chunk.locator["char_end"]) for chunk in chunks
    )
    previous_end = 1_000
    for start, end in ranges:
        assert start >= previous_end
        previous_end = end
