"""Extract evidence records that were actually visible in recorded tool messages."""

from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()
_WHITESPACE = " \t\r\n"
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = frozenset('"\\/bfnrt')


def extract_visible_records(messages: list[dict]) -> list[dict]:
    """Return visible evidence records from tool-role messages.

    Complete records retain their original fields. A record cut by production
    truncation contributes only the visible ``chunk_id`` and decoded snippet.
    """

    records: list[dict] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            # A directly recorded object may itself be a prefix.
            records.extend(_records_from_object_prefix(content))
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        rows = data.get("results")
        if isinstance(rows, list):
            records.extend(_complete_records(rows))
        preview = data.get("preview")
        if isinstance(preview, str):
            records.extend(_records_from_object_prefix(preview))
    return records


def _records_from_object_prefix(text: str) -> list[dict]:
    start = _skip_whitespace(text, 0)
    if start >= len(text) or text[start] != "{":
        return []

    data_start = _member_value_start(text, start, "data")
    if data_start is None:
        # A bounded preview contains the complete result.data object directly.
        data_start = start
    if data_start >= len(text) or text[data_start] != "{":
        return []

    results_start = _member_value_start(text, data_start, "results")
    if results_start is None or results_start >= len(text) or text[results_start] != "[":
        return []
    return _records_from_array_prefix(text, results_start)


def _member_value_start(text: str, object_start: int, target: str) -> int | None:
    position = _skip_whitespace(text, object_start)
    if position >= len(text) or text[position] != "{":
        return None
    position = _skip_whitespace(text, position + 1)

    while position < len(text) and text[position] != "}":
        try:
            key, position = _DECODER.raw_decode(text, position)
        except ValueError:
            return None
        if not isinstance(key, str):
            return None

        position = _skip_whitespace(text, position)
        if position >= len(text) or text[position] != ":":
            return None
        position = _skip_whitespace(text, position + 1)
        if key == target:
            return position

        try:
            _, position = _DECODER.raw_decode(text, position)
        except ValueError:
            return None
        position = _skip_whitespace(text, position)
        if position >= len(text) or text[position] != ",":
            return None
        position = _skip_whitespace(text, position + 1)
    return None


def _records_from_array_prefix(text: str, array_start: int) -> list[dict]:
    records: list[dict] = []
    position = _skip_whitespace(text, array_start + 1)

    while position < len(text) and text[position] != "]":
        try:
            value, end = _DECODER.raw_decode(text, position)
        except ValueError:
            partial = _partial_record(text, position)
            if partial is not None:
                records.append(partial)
            break

        if isinstance(value, dict):
            visible = _visible_complete_record(value)
            if visible is not None:
                records.append(visible)
        position = _skip_whitespace(text, end)
        if position >= len(text) or text[position] != ",":
            break
        position = _skip_whitespace(text, position + 1)
    return records


def _partial_record(text: str, object_start: int) -> dict | None:
    fields: dict[str, Any] = {}
    position = _skip_whitespace(text, object_start)
    if position >= len(text) or text[position] != "{":
        return None
    position = _skip_whitespace(text, position + 1)

    while position < len(text) and text[position] != "}":
        try:
            key, position = _DECODER.raw_decode(text, position)
        except ValueError:
            break
        if not isinstance(key, str):
            break

        position = _skip_whitespace(text, position)
        if position >= len(text) or text[position] != ":":
            break
        position = _skip_whitespace(text, position + 1)

        try:
            value, end = _DECODER.raw_decode(text, position)
        except ValueError:
            if key == "snippet":
                value, end = _string_prefix(text, position)
                fields[key] = value
            break

        fields[key] = value
        position = _skip_whitespace(text, end)
        if position >= len(text) or text[position] != ",":
            break
        position = _skip_whitespace(text, position + 1)

    chunk_id = fields.get("chunk_id")
    snippet = fields.get("snippet")
    if not isinstance(chunk_id, str) or not chunk_id:
        return None
    if not isinstance(snippet, str) or not snippet:
        return None
    return {"chunk_id": chunk_id, "snippet": snippet}


def _string_prefix(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != '"':
        return "", start

    pieces: list[str] = []
    position = start + 1
    while position < len(text):
        character = text[position]
        if character == '"':
            position += 1
            break
        if character == "\\":
            if position + 1 >= len(text):
                break
            escaped = text[position + 1]
            if escaped == "u":
                end = position + 6
                if end > len(text) or any(
                    character not in _HEX_DIGITS for character in text[position + 2 : end]
                ):
                    break
                pieces.append(text[position:end])
                position = end
                continue
            if escaped not in _SIMPLE_ESCAPES:
                break
            pieces.append(text[position : position + 2])
            position += 2
            continue
        if ord(character) < 0x20:
            break
        pieces.append(character)
        position += 1

    raw = "".join(pieces)
    try:
        decoded = json.loads(f'"{raw}"')
    except ValueError:
        return "", start
    # A cut UTF-16 surrogate pair is not a complete visible character.
    decoded = "".join(character for character in decoded if not "\ud800" <= character <= "\udfff")
    return decoded, position


def _complete_records(rows: list[Any]) -> list[dict]:
    records: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        visible = _visible_complete_record(row)
        if visible is not None:
            records.append(visible)
    return records


def _visible_complete_record(record: dict[str, Any]) -> dict[str, Any] | None:
    chunk_id = record.get("chunk_id")
    snippet = record.get("snippet")
    if not isinstance(chunk_id, str) or not chunk_id:
        return None
    if not isinstance(snippet, str) or not snippet:
        return None
    return record


def _skip_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position] in _WHITESPACE:
        position += 1
    return position


__all__ = ["extract_visible_records"]
