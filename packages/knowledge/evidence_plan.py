"""Pure assembly of model-readable evidence and its private source registry."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EVIDENCE_VIEW_FORMAT = "rag_view_v1"
_REF_FIELDS = {"snapshot_id", "knowledge_base_id", "document_revision_id", "chunk_id"}
_READING_FIELDS = {"snippet", "document_name", "effective_date", "superseded", "locator"}
_PAYLOAD_FIELDS = {
    "evidence_format",
    "status",
    "trust",
    "truncated",
    "citation_instruction",
    "results",
}
_ID = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]{0,31})?[1-9][0-9]*\Z")
_LOCATOR_POSITIONS = {"page", "char_start", "char_end", "paragraph_ordinal", "start", "end"}


def model_evidence_locator(value: Mapping) -> dict:
    """Project positional data, keeping arbitrary source metadata private."""
    result = {}
    for key in _LOCATOR_POSITIONS:
        if key in value:
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError("invalid evidence position")
            result[key] = value[key]
    if "type" in value:
        if value["type"] not in {"page", "text_range"}:
            raise ValueError("unsupported positional locator type")
        result["type"] = value["type"]
    return result


@dataclass(frozen=True)
class EvidencePlan:
    payload: dict[str, Any]
    registry: dict[str, dict[str, Any]]
    dropped: tuple[str, ...] = ()


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _size(value: Any, estimator: Callable[[str], int]) -> int:
    result = estimator(_json(value))
    if type(result) is not int or result < 0:
        raise ValueError("evidence estimator must return a non-negative integer")
    return result


def _budget(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("evidence budget must be a non-negative integer")


def _instruction(prefix: str) -> str:
    return f"Cite evidence using its id in square brackets, e.g. [{prefix}1]."


def _payload(items: list[dict], instruction: str, truncated: bool) -> dict:
    return {
        "evidence_format": EVIDENCE_VIEW_FORMAT,
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": truncated,
        "citation_instruction": instruction,
        "results": items,
    }


def _valid_item(item: Any) -> bool:
    if not isinstance(item, Mapping) or not {"id", "snippet"} <= set(item):
        return False
    if not set(item) <= _READING_FIELDS | {"id"}:
        return False
    if not isinstance(item["id"], str) or not _ID.fullmatch(item["id"]):
        return False
    if not isinstance(item["snippet"], str) or not item["snippet"].strip():
        return False
    for key in ("document_name", "effective_date"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            return False
    if "superseded" in item and type(item["superseded"]) is not bool:
        return False
    if "locator" in item and not isinstance(item["locator"], Mapping):
        return False
    try:
        if "locator" in item and model_evidence_locator(item["locator"]) != item["locator"]:
            return False
        _json(item)
    except (TypeError, ValueError):
        return False
    return True


def validate_evidence_view(payload: Any) -> bool:
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
        return False
    if (
        payload["evidence_format"] != EVIDENCE_VIEW_FORMAT
        or payload["status"] != "SUCCESS"
        or payload["trust"] != "UNTRUSTED"
        or type(payload["truncated"]) is not bool
        or not isinstance(payload["citation_instruction"], str)
        or not isinstance(payload["results"], list)
        or len(payload["results"]) > 10
    ):
        return False
    # Only a server-created instruction is valid, never arbitrary source text.
    match = re.fullmatch(
        r"Cite evidence using its id in square brackets, e\.g\. "
        r"\[((?:[A-Za-z][A-Za-z0-9_]{0,31})?)1\]\.",
        payload["citation_instruction"],
    )
    if match is None:
        return False
    prefix = match[1]
    seen = set()
    for item in payload["results"]:
        if not _valid_item(item) or item["id"] in seen:
            return False
        suffix = item["id"][len(prefix) :]
        if not item["id"].startswith(prefix) or not re.fullmatch(r"[1-9][0-9]*", suffix):
            return False
        seen.add(item["id"])
    return True


def _select(
    items: list[dict],
    *,
    budget: int,
    estimator: Callable[[str], int],
    instruction: str,
    was_truncated: bool = False,
    max_items: int = 10,
) -> tuple[dict | None, tuple[str, ...]]:
    kept, dropped = [], []
    for item in items:
        if len(kept) >= max_items:
            dropped.append(f"{item['id']}:count_limit")
            continue
        candidate = [*kept, item]
        # Reserve the larger serialization of both boolean states. The final
        # estimator therefore cannot exceed the budget when another item drops.
        fits = (
            max(
                _size(_payload(candidate, instruction, marker), estimator)
                for marker in (False, True)
            )
            <= budget
        )
        if fits:
            kept.append(item)
        else:
            dropped.append(f"{item['id']}:budget")
    result = _payload(kept, instruction, was_truncated or bool(dropped))
    return (result if _size(result, estimator) <= budget else None), tuple(dropped)


def assemble_evidence_plan(
    records: Sequence[Mapping],
    references: Sequence[Mapping],
    *,
    budget: int,
    estimator: Callable[[str], int],
    citation_prefix: str = "E",
    max_items: int = 10,
) -> EvidencePlan | None:
    _budget(budget)
    if (
        not isinstance(citation_prefix, str)
        or (citation_prefix and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", citation_prefix))
        or type(max_items) is not int
        or not 1 <= max_items <= 10
    ):
        raise ValueError("invalid evidence citation prefix or count")
    if (
        not isinstance(records, (list, tuple))
        or not isinstance(references, (list, tuple))
        or len(records) > 100
        or len(records) != len(references)
    ):
        return None
    indexed: dict[tuple, dict] = {}
    try:
        for ref in references:
            if (
                not isinstance(ref, Mapping)
                or set(ref) != _REF_FIELDS
                or any(not isinstance(ref[key], str) or not ref[key] for key in _REF_FIELDS)
            ):
                return None
            identity = (ref["chunk_id"], ref["document_revision_id"], ref["snapshot_id"])
            if identity in indexed:
                return None
            indexed[identity] = dict(ref)
        items, registry, seen = [], {}, set()
        for ordinal, record in enumerate(records, start=1):
            if not isinstance(record, Mapping):
                return None
            candidates = [
                key
                for key, ref in indexed.items()
                if record.get("chunk_id") == ref["chunk_id"]
                and record.get("document_revision_id") == ref["document_revision_id"]
                and ("snapshot_id" not in record or record["snapshot_id"] == ref["snapshot_id"])
                and (
                    "knowledge_base_id" not in record
                    or record["knowledge_base_id"] == ref["knowledge_base_id"]
                )
            ]
            if len(candidates) != 1 or candidates[0] in seen:
                return None
            identity = candidates[0]
            seen.add(identity)
            identifier = f"{citation_prefix}{ordinal}"
            item = {"id": identifier, **{k: record[k] for k in _READING_FIELDS if k in record}}
            if "locator" in item:
                if not isinstance(item["locator"], Mapping):
                    return None
                item["locator"] = model_evidence_locator(item["locator"])
            if not _valid_item(item):
                return None
            # Include non-model fields in validation, too: source records must
            # be JSON-safe even though their full identities leave the prompt.
            _json(record)
            items.append(item)
            registry[identifier] = {"reference": indexed[identity], "record": dict(record)}
        if seen != set(indexed):
            return None
    except (TypeError, ValueError):
        return None
    payload, dropped = _select(
        items,
        budget=budget,
        estimator=estimator,
        instruction=_instruction(citation_prefix),
        max_items=max_items,
    )
    if payload is None:
        return None
    selected = {item["id"] for item in payload["results"]}
    return EvidencePlan(
        payload, {key: value for key, value in registry.items() if key in selected}, dropped
    )


def reproject_evidence_view(
    payload: Mapping, *, budget: int, estimator: Callable[[str], int]
) -> dict | None:
    _budget(budget)
    if not validate_evidence_view(payload):
        return None
    result, _ = _select(
        payload["results"],
        budget=budget,
        estimator=estimator,
        instruction=payload["citation_instruction"],
        was_truncated=payload["truncated"],
    )
    return result
