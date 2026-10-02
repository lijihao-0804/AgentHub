"""Bounded structural summaries; never retain argument values or result bodies."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from uuid import UUID

ARGUMENT_KEYS = frozenset(
    {
        "query",
        "limit",
        "expression",
        "customer_ref",
        "subject",
        "description",
        "priority",
        "status",
        "customer_id",
        "ticket_id",
    }
)
KINDS = frozenset({"string", "number", "boolean", "null", "object", "array"})


def argument_summary(arguments: Mapping) -> str:
    def kind(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, list):
            return "array"
        return "object"

    fields = {key: kind(value) for key, value in arguments.items() if key in ARGUMENT_KEYS}
    return json.dumps(
        {"fields": fields, "hidden_count": min(10000, len(arguments) - len(fields))}, sort_keys=True
    )


def validate_summary(value: str) -> str:
    if len(value) > 2048:
        raise ValueError("unsafe argument summary")
    data = json.loads(value)
    if not isinstance(data, dict) or set(data) != {"fields", "hidden_count"}:
        raise ValueError("unsafe argument summary")
    fields = data["fields"]
    if (
        not isinstance(fields, dict)
        or not set(fields) <= ARGUMENT_KEYS
        or any(not isinstance(v, str) or v not in KINDS for v in fields.values())
    ):
        raise ValueError("unsafe argument summary")
    if type(data["hidden_count"]) is not int or not 0 <= data["hidden_count"] <= 10000:
        raise ValueError("unsafe argument summary")
    return value


def evidence_refs(data: object) -> tuple[dict[str, str], ...]:
    raw = data.get("evidence_refs", []) if isinstance(data, dict) else []
    if not isinstance(raw, list):
        return ()
    result = []
    for item in raw[:10]:
        if not isinstance(item, dict) or set(item) != {
            "snapshot_id",
            "knowledge_base_id",
            "document_revision_id",
            "chunk_id",
        }:
            continue
        try:
            safe = {
                k: str(UUID(item[k]))
                for k in ("snapshot_id", "knowledge_base_id", "document_revision_id")
            }
            chunk = item["chunk_id"]
            if not isinstance(chunk, str) or not re.fullmatch(r"[A-Za-z0-9_:\-]{1,128}", chunk):
                continue
            safe["chunk_id"] = chunk
            result.append(safe)
        except (ValueError, TypeError, AttributeError):
            continue
    return tuple(result)


def validate_evidence(value: str) -> str:
    if len(value) > 4096:
        raise ValueError("unsafe evidence references")
    raw = json.loads(value)
    safe = evidence_refs({"evidence_refs": raw})
    if not isinstance(raw, list) or len(safe) != len(raw) or len(raw) > 10:
        raise ValueError("unsafe evidence references")
    return value
