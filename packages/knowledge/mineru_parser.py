"""Import MinerU's flat content_list or a completed single-document /file_parse result."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from packages.core.canonical.json_parse import load_unique_json
from packages.knowledge.parser_types import (
    MAX_STRUCTURED_BLOCKS,
    DocumentParseError,
    ParsedBlock,
    structure_limit,
)
from packages.knowledge.table_parser import html_table_blocks

_TEXT_TYPES = {"text", "equation", "header", "footer", "page_number", "aside_text", "page_footnote"}
_VISUAL_TYPES = {"table", "image", "chart", "code"}


def _bad_content() -> DocumentParseError:
    return DocumentParseError(
        "PARSE_FAILED", "Expected MinerU's flat *_content_list.json with typed blocks and page_idx."
    )


def _json(text: str) -> Any:
    try:
        return load_unique_json(text)
    except (ValueError, RecursionError) as exc:
        raise DocumentParseError("PARSE_FAILED", "The MinerU result is not valid JSON.") from exc


def _content_list(text: str) -> list:
    content = _json(text)
    if isinstance(content, dict):
        results = content.get("results")
        if not isinstance(results, dict) or not results:
            raise DocumentParseError(
                "PARSE_FAILED",
                "Upload the *_content_list.json file from the MinerU result archive, "
                "or a completed /file_parse response containing content_list.",
            )
        if len(results) != 1:
            raise DocumentParseError("PARSE_FAILED", "Import one MinerU document per revision.")
        result = next(iter(results.values()))
        if not isinstance(result, dict) or "content_list" not in result:
            raise _bad_content()
        content = result["content_list"]
        if isinstance(content, str):
            content = _json(content)
    if not isinstance(content, list):
        raise _bad_content()
    if len(content) > MAX_STRUCTURED_BLOCKS:
        raise structure_limit()
    return content


def _strings(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value)
    raise _bad_content()


def _visual_parts(item: dict, locator: dict, max_chars: int) -> Iterator[ParsedBlock]:
    kind = item["type"]
    caption = _strings(item.get(f"{kind}_caption", []))
    if caption.strip():
        yield ParsedBlock(caption, {**locator, "block_kind": "caption", "part_index": 0})
    body_key = {"table": "table_body", "code": "code_body"}.get(kind, "content")
    body = _strings(item.get(body_key, ""))
    if body.strip():
        body_locator = {**locator, "block_kind": kind, "part_index": 1}
        if kind == "table":
            yield from html_table_blocks(body, body_locator, max_chars)
        else:
            yield ParsedBlock(body, body_locator)
    footnote = _strings(item.get(f"{kind}_footnote", []))
    if footnote.strip():
        yield ParsedBlock(footnote, {**locator, "block_kind": "footnote", "part_index": 2})


def mineru_blocks(text: str, *, max_pages: int, max_chars: int) -> Iterator[ParsedBlock]:
    page_blocks: Counter[int] = Counter()
    for item in _content_list(text):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise _bad_content()
        page_idx = item.get("page_idx")
        if type(page_idx) is not int or page_idx < 0:
            raise _bad_content()
        if page_idx >= max_pages:
            raise DocumentParseError(
                "RESOURCE_LIMIT_EXCEEDED", "The MinerU result exceeds the configured page limit."
            )
        locator = {"type": "page", "page": page_idx + 1, "block_index": page_blocks[page_idx]}
        page_blocks[page_idx] += 1
        if "bbox" in item:
            box = item["bbox"]
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(type(n) is not int or not 0 <= n <= 1000 for n in box)
                or box[0] > box[2]
                or box[1] > box[3]
            ):
                raise _bad_content()
            locator["bbox"] = box
        kind = item["type"]
        if kind in _VISUAL_TYPES:
            yield from _visual_parts(item, locator, max_chars)
            continue
        if kind == "list":
            body = _strings(item.get("list_items"))
        elif kind in _TEXT_TYPES:
            body = _strings(item.get("text"))
        else:
            raise _bad_content()
        if kind == "text":
            level = item.get("text_level", 0)
            if type(level) is not int or level < 0:
                raise _bad_content()
            if level:
                kind = "heading"
                locator["heading_level"] = level
        if body.strip():
            yield ParsedBlock(body, {**locator, "block_kind": kind})
