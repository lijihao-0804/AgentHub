"""Killable, provider-neutral document parsing contracts and adapters."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    locator: dict[str, Any]


@dataclass(frozen=True)
class ParsedDocument:
    blocks: tuple[ParsedBlock, ...]


class DocumentParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DocumentParser(Protocol):
    async def parse(self, content: bytes, *, media_type: str) -> ParsedDocument: ...


class _ParserFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_content(
    content: bytes,
    media_type: str,
    *,
    max_pdf_pages: int,
    max_parsed_chars: int,
) -> ParsedDocument:
    if media_type in {"text/plain", "text/markdown"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _ParserFailure("PARSE_FAILED", "The text file is not valid UTF-8.") from exc
        if "\x00" in text:
            raise _ParserFailure("PARSE_FAILED", "The text file contains invalid characters.")
        if len(text) > max_parsed_chars:
            raise _ParserFailure(
                "RESOURCE_LIMIT_EXCEEDED", "The parsed document exceeds the configured limit."
            )
        return ParsedDocument(
            blocks=(
                ParsedBlock(
                    text=text,
                    locator={"type": "text_range", "char_start": 0, "char_end": len(text)},
                ),
            )
        )

    if media_type != "application/pdf":
        raise _ParserFailure("UNSUPPORTED_FILE", "The document type is not supported.")

    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=False)
        if len(reader.pages) > max_pdf_pages:
            raise _ParserFailure(
                "RESOURCE_LIMIT_EXCEEDED", "The PDF exceeds the configured page limit."
            )
        blocks: list[ParsedBlock] = []
        parsed_chars = 0
        for page_number, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            parsed_chars += len(page_text)
            if parsed_chars > max_parsed_chars:
                raise _ParserFailure(
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
    except _ParserFailure:
        raise
    except Exception as exc:
        raise _ParserFailure("PARSE_FAILED", "The PDF could not be parsed.") from exc

    if not blocks:
        raise _ParserFailure("PARSE_FAILED", "The document contains no extractable text.")
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
