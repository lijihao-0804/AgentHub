"""Killable, provider-neutral document parsing contracts and adapters."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import Protocol

from packages.knowledge.mineru_parser import mineru_blocks
from packages.knowledge.parser_types import (
    MAX_STRUCTURED_BLOCKS,
    MAX_TABLE_CELLS,
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
    structure_limit,
    text_limit,
)
from packages.knowledge.table_parser import delimited_blocks

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


class DocumentParser(Protocol):
    async def parse(self, content: bytes, *, media_type: str) -> ParsedDocument: ...


def _structured_document(blocks: Iterable[ParsedBlock], max_chars: int) -> ParsedDocument:
    collected = []
    chars = cells = 0
    for block in blocks:
        chars += len(block.text)
        if chars > max_chars:
            raise text_limit()
        cells += len(block.locator.get("cells", ()))
        if len(collected) >= MAX_STRUCTURED_BLOCKS or cells > MAX_TABLE_CELLS:
            raise structure_limit()
        # JSON escapes can introduce NULs or lone surrogates even after UTF-8 decoding.
        if "\x00" in block.text:
            raise DocumentParseError("PARSE_FAILED", "The parsed text contains invalid characters.")
        try:
            block.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DocumentParseError(
                "PARSE_FAILED", "The parsed text is not valid Unicode."
            ) from exc
        collected.append(block)
    if not collected:
        raise DocumentParseError("PARSE_FAILED", "The document contains no usable text or cells.")
    return ParsedDocument(tuple(collected))


def _parse_content(
    content: bytes,
    media_type: str,
    *,
    max_pdf_pages: int,
    max_parsed_chars: int,
) -> ParsedDocument:
    if media_type in {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
    }:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentParseError("PARSE_FAILED", "The text file is not valid UTF-8.") from exc
        if "\x00" in text:
            raise DocumentParseError("PARSE_FAILED", "The text file contains invalid characters.")
        if media_type == "application/json":
            return _structured_document(
                mineru_blocks(text, max_pages=max_pdf_pages, max_chars=max_parsed_chars),
                max_parsed_chars,
            )
        if media_type in {"text/csv", "text/tab-separated-values"}:
            return _structured_document(
                delimited_blocks(
                    text,
                    delimiter="," if media_type == "text/csv" else "\t",
                    max_chars=max_parsed_chars,
                ),
                max_parsed_chars,
            )
        if len(text) > max_parsed_chars:
            raise text_limit()
        return ParsedDocument(
            blocks=(
                ParsedBlock(
                    text=text,
                    locator={"type": "text_range", "char_start": 0, "char_end": len(text)},
                ),
            )
        )

    if media_type != "application/pdf":
        raise DocumentParseError("UNSUPPORTED_FILE", "The document type is not supported.")

    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=False)
        if len(reader.pages) > max_pdf_pages:
            raise DocumentParseError(
                "RESOURCE_LIMIT_EXCEEDED", "The PDF exceeds the configured page limit."
            )
        blocks: list[ParsedBlock] = []
        parsed_chars = 0
        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            parsed_chars += len(page_text)
            if parsed_chars > max_parsed_chars:
                raise DocumentParseError(
                    "RESOURCE_LIMIT_EXCEEDED",
                    "The parsed document exceeds the configured limit.",
                )
            if page_text:
                blocks.append(
                    ParsedBlock(
                        text=page_text,
                        locator={"type": "page", "page": page_number},
                    )
                )
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("PARSE_FAILED", "The PDF could not be parsed.") from exc

    if not blocks:
        raise DocumentParseError(
            "PARSE_FAILED",
            "The PDF contains no extractable text. Upload a MinerU content list for scanned pages.",
        )
    return ParsedDocument(blocks=tuple(blocks))


class ProcessDocumentParser:
    """Run untrusted parsing in a killable subprocess."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_pdf_pages: int,
        max_parsed_chars: int,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_pdf_pages = max_pdf_pages
        self.max_parsed_chars = max_parsed_chars

    async def parse(self, content: bytes, *, media_type: str) -> ParsedDocument:
        return await asyncio.to_thread(self._parse, content, media_type)

    def _parse(self, content: bytes, media_type: str) -> ParsedDocument:
        input_path: Path | None = None
        result_path: Path | None = None
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as input_file:
            input_path = Path(input_file.name)
            input_file.write(content)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as result_file:
            result_path = Path(result_file.name)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (_PROJECT_ROOT, env.get("PYTHONPATH", "")) if part
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "packages.knowledge.parser_child",
                os.fspath(input_path),
                os.fspath(result_path),
                media_type,
                str(self.max_pdf_pages),
                str(self.max_parsed_chars),
            ],
            cwd=_PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            try:
                process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                raise DocumentParseError(
                    "PARSE_TIMEOUT", "Document parsing exceeded the time limit."
                ) from exc
            if process.returncode != 0:
                raise DocumentParseError("PARSE_FAILED", "The document could not be parsed.")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DocumentParseError(
                    "PARSE_FAILED", "The document could not be parsed."
                ) from exc
        finally:
            if input_path is not None:
                input_path.unlink(missing_ok=True)
            if result_path is not None:
                result_path.unlink(missing_ok=True)

        if not result.get("ok"):
            raise DocumentParseError(result["code"], result["message"])
        return ParsedDocument(
            blocks=tuple(
                ParsedBlock(text=item["text"], locator=dict(item["locator"]))
                for item in result["blocks"]
            )
        )


__all__ = [
    "DocumentParseError",
    "DocumentParser",
    "ParsedBlock",
    "ParsedDocument",
    "ProcessDocumentParser",
]
