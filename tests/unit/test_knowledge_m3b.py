from __future__ import annotations

from io import BytesIO
from uuid import UUID, uuid4

import pytest
from pypdf import PdfWriter

from packages.knowledge.adapters.celery_queue import CeleryIngestionQueue
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import (
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
    ProcessDocumentParser,
)


class RecordingCelery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], bool]] = []

    def send_task(self, name: str, *, args: list[str], ignore_result: bool) -> None:
        self.calls.append((name, args, ignore_result))


@pytest.mark.asyncio
async def test_celery_queue_transports_only_job_id() -> None:
    celery = RecordingCelery()
    queue = CeleryIngestionQueue(celery)  # type: ignore[arg-type]
    job_id = uuid4()

    await queue.enqueue(job_id)

    assert celery.calls == [("agenthub.process_knowledge_ingestion", [str(job_id)], True)]


@pytest.mark.asyncio
async def test_text_parser_returns_provider_neutral_blocks() -> None:
    parser = ProcessDocumentParser(
        timeout_seconds=10,
        max_pdf_pages=10,
        max_parsed_chars=1_000,
    )

    parsed = await parser.parse("第一行\r\n第二行".encode(), media_type="text/plain")

    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].text == "第一行\r\n第二行"
    assert parsed.blocks[0].locator == {"type": "text_range", "char_start": 0, "char_end": 8}


@pytest.mark.asyncio
async def test_parser_rejects_unsupported_type_without_leaking_provider_errors() -> None:
    parser = ProcessDocumentParser(
        timeout_seconds=10,
        max_pdf_pages=10,
        max_parsed_chars=1_000,
    )

    with pytest.raises(DocumentParseError) as error:
        await parser.parse(b"content", media_type="application/zip")

    assert error.value.code == "UNSUPPORTED_FILE"
    assert "pypdf" not in str(error.value)


@pytest.mark.asyncio
async def test_parser_supports_markdown_and_enforces_text_limit() -> None:
    parser = ProcessDocumentParser(
        timeout_seconds=10,
        max_pdf_pages=10,
        max_parsed_chars=100,
    )

    parsed = await parser.parse(b"# heading", media_type="text/markdown")

    assert parsed.blocks[0].text == "# heading"
    assert parsed.blocks[0].locator["type"] == "text_range"

    limited = ProcessDocumentParser(
        timeout_seconds=10,
        max_pdf_pages=10,
        max_parsed_chars=3,
    )
    with pytest.raises(DocumentParseError) as error:
        await limited.parse(b"four", media_type="text/plain")
    assert error.value.code == "RESOURCE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_pdf_page_limit_is_enforced_in_isolated_parser() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    pdf = BytesIO()
    writer.write(pdf)
    parser = ProcessDocumentParser(
        timeout_seconds=10,
        max_pdf_pages=1,
        max_parsed_chars=100,
    )

    with pytest.raises(DocumentParseError) as error:
        await parser.parse(pdf.getvalue(), media_type="application/pdf")
    assert error.value.code == "RESOURCE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_parser_timeout_terminates_child_process() -> None:
    parser = ProcessDocumentParser(
        timeout_seconds=0.001,
        max_pdf_pages=10,
        max_parsed_chars=100,
    )

    with pytest.raises(DocumentParseError) as error:
        await parser.parse(b"timeout candidate", media_type="text/plain")
    assert error.value.code == "PARSE_TIMEOUT"


def test_chunk_ids_and_content_are_deterministic() -> None:
    revision_id = UUID("00000000-0000-0000-0000-000000000001")
    document = ParsedDocument(
        blocks=(ParsedBlock(text="alpha beta gamma delta", locator={"type": "text_range"}),)
    )

    first = build_deterministic_chunks(
        revision_id,
        document,
        chunk_size_chars=10,
        chunk_overlap_chars=2,
    )
    second = build_deterministic_chunks(
        revision_id,
        document,
        chunk_size_chars=10,
        chunk_overlap_chars=2,
    )

    assert first == second
    assert [chunk.ordinal for chunk in first] == list(range(len(first)))
    assert all(len(chunk.normalized_content_hash) == 64 for chunk in first)
    assert all(chunk.chunk_id for chunk in first)


def test_fixed_window_locators_add_block_base_offset() -> None:
    # Two blocks of one document, each with its own text_range base: the
    # second block's chunk windows must be offset by its base, or chunks of
    # different blocks collide on overlapping coordinate ranges.
    document = ParsedDocument(
        blocks=(
            ParsedBlock(
                text="a" * 1_500,
                locator={"type": "text_range", "char_start": 0, "char_end": 1_500},
            ),
            ParsedBlock(
                text="b" * 1_500,
                locator={"type": "text_range", "char_start": 1_500, "char_end": 3_000},
            ),
        )
    )

    chunks = build_deterministic_chunks(
        uuid4(),
        document,
        chunk_size_chars=1_200,
        chunk_overlap_chars=200,
    )

    spans = [(chunk.locator["char_start"], chunk.locator["char_end"]) for chunk in chunks]
    first_block = [span for span in spans if span[1] <= 1_500]
    second_block = [span for span in spans if span[0] >= 1_500]
    assert first_block and second_block
    assert all(span[0] < 1_500 for span in first_block)
    assert all(span[0] >= 1_500 for span in second_block)
    # Windows stay inside their own block's base range.
    assert all(span[1] <= 3_000 for span in second_block)
