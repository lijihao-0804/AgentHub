"""Pure assembly of model-readable evidence and its private source registry."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from packages.knowledge.contracts import EvidenceOrigin

EVIDENCE_VIEW_FORMAT = "rag_view_v1"
_REF_FIELDS = {"snapshot_id", "knowledge_base_id", "document_revision_id", "chunk_id"}
_READING_FIELDS = {"snippet", "document_name", "effective_date", "superseded", "locator"}
# Origin-layer markers travel into the model view: a mixed delivery must stay
# judgeable at the model. ``wiki_page_id`` only ever accompanies a
# WIKI_DERIVED item; publication identity stays in the private registry.
_ORIGIN_FIELDS = {"origin", "wiki_page_id"}
_ITEM_FIELDS = _READING_FIELDS | _ORIGIN_FIELDS | {"conflict"}
_LEGACY_PAYLOAD_FIELDS = {
    "evidence_format",
    "status",
    "trust",
    "truncated",
    "citation_instruction",
    "results",
}
_SERVER_STATE_FIELDS = {
    "route",
    "insufficient_evidence",
    "supplemental_required",
    "conflict_count",
    "degraded",
    "degraded_reason",
    "delivered_origins",
}
_PAYLOAD_FIELDS = _LEGACY_PAYLOAD_FIELDS | _SERVER_STATE_FIELDS
_ROUTES = {"AUTO", "WIKI", "RAG", "HYBRID"}
_STATUSES = {"SUCCESS", "INSUFFICIENT_EVIDENCE", "CONFLICT", "DEGRADED"}
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


def _infer_route(items: Sequence[Mapping]) -> str:
    origins = {item.get("origin") for item in items}
    has_wiki = EvidenceOrigin.WIKI_DERIVED.value in origins
    has_raw = EvidenceOrigin.RAW.value in origins
    if has_wiki and has_raw:
        return "HYBRID"
    if has_wiki:
        return "WIKI"
    if has_raw:
        return "RAG"
    return "RAG"


def _normalized_server_state(
    value: Mapping[str, Any] | None,
    items: Sequence[Mapping],
    *,
    was_truncated: bool,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not set(value).issubset(
        {
            "route",
            "insufficient_evidence",
            "supplemental_required",
            "conflict_count",
            "degraded",
            "degraded_reason",
        }
    ):
        raise ValueError("invalid server-owned evidence state")
    route = value.get("route", _infer_route(items))
    if not isinstance(route, str) or route not in _ROUTES:
        raise ValueError("invalid evidence route")
    insufficient = value.get("insufficient_evidence", False)
    supplemental = value.get("supplemental_required", False)
    if type(insufficient) is not bool or type(supplemental) is not bool:
        raise ValueError("invalid evidence sufficiency state")
    conflict_count = value.get("conflict_count", 0)
    if type(conflict_count) is not int or conflict_count < 0:
        raise ValueError("invalid evidence conflict count")
    degraded = value.get("degraded", False)
    reason = value.get("degraded_reason")
    if type(degraded) is not bool or (
        reason is not None and (not isinstance(reason, str) or not reason.strip())
    ):
        raise ValueError("invalid evidence degradation state")

    delivered = {
        item.get("origin") for item in items if item.get("origin") in {"WIKI_DERIVED", "RAW"}
    }
    if route == "HYBRID":
        expected = {"WIKI_DERIVED", "RAW"}
        # A server answer may say HYBRID at the retriever boundary even when
        # only one origin produced evidence, and budget projection can remove
        # one of two originally available origins. In either case the payload
        # must not continue to imply that both sides were delivered.
        if delivered != expected and not degraded:
            degraded = True
            reason = reason or "HYBRID_PARTIAL_DELIVERY"
    if was_truncated and not degraded:
        degraded = True
        reason = reason or "EVIDENCE_TRUNCATED"
    if conflict_count:
        status = "CONFLICT"
    elif insufficient or supplemental:
        status = "INSUFFICIENT_EVIDENCE"
    elif degraded:
        status = "DEGRADED"
    else:
        status = "SUCCESS"
    return {
        "route": route,
        "insufficient_evidence": insufficient,
        "supplemental_required": supplemental,
        "conflict_count": conflict_count,
        "degraded": degraded,
        "degraded_reason": reason,
        "delivered_origins": sorted(delivered),
        "status": status,
    }


def _payload(
    items: list[dict],
    instruction: str,
    truncated: bool,
    *,
    server_state: Mapping[str, Any] | None = None,
) -> dict:
    payload = {
        "evidence_format": EVIDENCE_VIEW_FORMAT,
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": truncated,
        "citation_instruction": instruction,
        "results": items,
    }
    state = _normalized_server_state(server_state, items, was_truncated=truncated)
    if state is not None:
        payload["status"] = state["status"]
        for key in _SERVER_STATE_FIELDS:
            if key != "status":
                payload[key] = state[key]
    return payload


def _valid_item(item: Any) -> bool:
    if not isinstance(item, Mapping) or not {"id", "snippet"} <= set(item):
        return False
    if not set(item) <= _ITEM_FIELDS | {"id"}:
        return False
    if not isinstance(item["id"], str) or not _ID.fullmatch(item["id"]):
        return False
    if not isinstance(item["snippet"], str) or not item["snippet"].strip():
        return False
    if "origin" in item:
        if item["origin"] not in {value.value for value in EvidenceOrigin}:
            return False
        if item["origin"] == EvidenceOrigin.WIKI_DERIVED.value and (
            not isinstance(item.get("wiki_page_id"), str) or not item["wiki_page_id"].strip()
        ):
            return False
    if "wiki_page_id" in item and item.get("origin") != EvidenceOrigin.WIKI_DERIVED.value:
        return False
    for key in ("document_name", "effective_date"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            return False
    if "superseded" in item and type(item["superseded"]) is not bool:
        return False
    if "conflict" in item and type(item["conflict"]) is not bool:
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
    if not isinstance(payload, Mapping):
        return False
    payload_fields = set(payload)
    if payload_fields != _LEGACY_PAYLOAD_FIELDS and payload_fields != _PAYLOAD_FIELDS:
        return False
    rich = payload_fields == _PAYLOAD_FIELDS
    if (
        payload["evidence_format"] != EVIDENCE_VIEW_FORMAT
        or payload["status"] not in _STATUSES
        or payload["trust"] != "UNTRUSTED"
        or type(payload["truncated"]) is not bool
        or not isinstance(payload["citation_instruction"], str)
        or not isinstance(payload["results"], list)
        or len(payload["results"]) > 10
    ):
        return False
    if not rich and payload["status"] != "SUCCESS":
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
    if rich:
        try:
            state = _normalized_server_state(
                {
                    "route": payload["route"],
                    "insufficient_evidence": payload["insufficient_evidence"],
                    "supplemental_required": payload["supplemental_required"],
                    "conflict_count": payload["conflict_count"],
                    "degraded": payload["degraded"],
                    "degraded_reason": payload["degraded_reason"],
                },
                payload["results"],
                was_truncated=payload["truncated"],
            )
        except ValueError:
            return False
        if state is None or state["status"] != payload["status"]:
            return False
        if payload["delivered_origins"] != state["delivered_origins"]:
            return False
    return True


def _select(
    items: list[dict],
    *,
    budget: int,
    estimator: Callable[[str], int],
    instruction: str,
    was_truncated: bool = False,
    max_items: int = 10,
    server_state: Mapping[str, Any] | None = None,
) -> tuple[dict | None, tuple[str, ...]]:
    kept, dropped = [], []
    ordered: list[dict] = []
    route = server_state.get("route") if isinstance(server_state, Mapping) else None
    if route == "HYBRID":
        # Reserve one slot each for the two independently produced evidence
        # layers before filling the remaining capacity. A retriever hit is not
        # the same as a delivered origin: if one side still cannot fit, the
        # resulting payload is explicitly marked degraded below.
        for origin in (EvidenceOrigin.WIKI_DERIVED.value, EvidenceOrigin.RAW.value):
            match = next((item for item in items if item.get("origin") == origin), None)
            if match is not None:
                ordered.append(match)
    ordered.extend(item for item in items if item not in ordered)
    for item in ordered:
        if len(kept) >= max_items:
            dropped.append(f"{item['id']}:count_limit")
            continue
        candidate = [*kept, item]
        # Reserve the larger serialization of both boolean states. The final
        # estimator therefore cannot exceed the budget when another item drops.
        fits = (
            max(
                _size(
                    _payload(
                        candidate,
                        instruction,
                        marker,
                        server_state=server_state,
                    ),
                    estimator,
                )
                for marker in (False, True)
            )
            <= budget
        )
        if fits:
            kept.append(item)
        else:
            dropped.append(f"{item['id']}:budget")
    result = _payload(
        kept,
        instruction,
        was_truncated or bool(dropped),
        server_state=server_state,
    )
    return (result if _size(result, estimator) <= budget else None), tuple(dropped)


def assemble_evidence_plan(
    records: Sequence[Mapping],
    references: Sequence[Mapping],
    *,
    budget: int,
    estimator: Callable[[str], int],
    citation_prefix: str = "E",
    max_items: int = 10,
    record_validator: Callable[[Mapping], str | None] | None = None,
    allow_shared_references: bool = False,
    server_state: Mapping[str, Any] | None = None,
) -> EvidencePlan | None:
    """Assemble the model view and its private registry.

    ``record_validator`` optionally enforces a source-layer envelope on every
    record before anything is registered: a non-None return value is the
    violation reason and fails the whole plan closed, so an incomplete
    WIKI_DERIVED record can never reach the model or the registry.

    ``allow_shared_references`` admits several records onto one source
    reference (a Wiki page may hold two claims quoted from the same chunk).
    The default stays the strict search_knowledge pairing: one record, one
    reference, no ambiguity.

    ``server_state`` is an optional keyword-only projection of trusted route,
    sufficiency, conflict and degradation decisions made outside the model
    view. Supplying it adds those server-owned fields and derives ``status``
    from them; reprojection preserves the same state.
    """

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
    if server_state is not None and not isinstance(server_state, Mapping):
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
            if identity in indexed and not allow_shared_references:
                return None
            indexed.setdefault(identity, dict(ref))
        items, registry, seen = [], {}, set()
        for ordinal, record in enumerate(records, start=1):
            if not isinstance(record, Mapping):
                return None
            if record_validator is not None and record_validator(record) is not None:
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
            if len(candidates) != 1:
                return None
            identity = candidates[0]
            if not allow_shared_references and identity in seen:
                return None
            seen.add(identity)
            identifier = f"{citation_prefix}{ordinal}"
            item = {"id": identifier, **{k: record[k] for k in _ITEM_FIELDS if k in record}}
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
        if not allow_shared_references and seen != set(indexed):
            return None
    except (TypeError, ValueError):
        return None
    payload, dropped = _select(
        items,
        budget=budget,
        estimator=estimator,
        instruction=_instruction(citation_prefix),
        max_items=max_items,
        server_state=server_state,
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
        server_state=(
            {
                "route": payload["route"],
                "insufficient_evidence": payload["insufficient_evidence"],
                "supplemental_required": payload["supplemental_required"],
                "conflict_count": payload["conflict_count"],
                "degraded": payload["degraded"],
                "degraded_reason": payload["degraded_reason"],
            }
            if set(payload) == _PAYLOAD_FIELDS
            else None
        ),
    )
    return result
