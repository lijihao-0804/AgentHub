"""Lossless, reference-paired projections for bounded RAG evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

EVIDENCE_FORMAT = "rag_records_v1"
_REFERENCE_FIELDS = frozenset(
    {"snapshot_id", "knowledge_base_id", "document_revision_id", "chunk_id"}
)
Reference = dict[str, Any]


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int:
        """Return a non-negative token estimate for text."""


Estimator = Callable[[str], int] | TokenEstimator


def project_rag_evidence_payload(
    payload: Mapping[str, Any],
    budget: int,
    estimator: Estimator | None = None,
) -> dict[str, Any] | None:
    """Project a successful search payload into complete, paired records.

    The helper never truncates a record or synthesizes a citation. It keeps the
    original records and their matching references in source order, skipping a
    record that cannot fit so later records may still be admitted. Invalid or
    ambiguous identity data fails closed with ``None``.
    """

    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError("budget must be a non-negative integer")
    if budget == 0 or not isinstance(payload, Mapping):
        return None
    if payload.get("status") != "SUCCESS":
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    records = data.get("results")
    references = data.get("evidence_refs")
    if not isinstance(records, list) or not isinstance(references, list):
        return None

    normalized_refs = _index_references(references)
    if normalized_refs is None:
        return None
    normalized_records = _index_records(records, normalized_refs)
    if normalized_records is None:
        return None

    estimate = _estimator(estimator)
    selected_records: list[dict[str, Any]] = []
    selected_refs: list[Reference] = []
    skipped = False
    for record, reference in normalized_records:
        candidate_records = [*selected_records, record]
        candidate_refs = [*selected_refs, reference]
        candidate = _payload(candidate_records, candidate_refs, truncated=skipped)
        if _estimate_payload(candidate, estimate) <= budget:
            selected_records = candidate_records
            selected_refs = candidate_refs
        else:
            skipped = True

    if not selected_records:
        empty = _payload([], [], truncated=bool(records or references))
        return empty if _estimate_payload(empty, estimate) <= budget else None
    return _payload(selected_records, selected_refs, truncated=skipped)


def reproject_rag_evidence_payload(
    payload: Mapping[str, Any],
    budget: int,
    estimator: Estimator,
) -> dict[str, Any] | None:
    """Validate and tighten an existing helper payload without repairing it."""

    if (
        payload.get("evidence_format") != EVIDENCE_FORMAT
        or payload.get("status") != "SUCCESS"
        or type(payload.get("truncated")) is not bool
    ):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    records = data.get("results")
    references = data.get("evidence_refs")
    if not isinstance(records, list) or not isinstance(references, list):
        return None
    projected = project_rag_evidence_payload(
        {
            "status": "SUCCESS",
            "data": {"results": records, "evidence_refs": references},
        },
        budget,
        estimator,
    )
    if projected is None:
        return None
    data = projected["data"]
    return _payload(
        data["results"],
        data["evidence_refs"],
        truncated=payload["truncated"] or projected["truncated"],
    )


def _estimator(estimator: Estimator | None) -> Callable[[str], int]:
    if estimator is None:
        return lambda text: len(text.encode("utf-8"))
    if callable(estimator):
        return estimator
    method = getattr(estimator, "estimate", None)
    if not callable(method):
        raise TypeError("estimator must be callable or implement estimate(text)")
    return method


def _estimate_payload(payload: Mapping[str, Any], estimator: Callable[[str], int]) -> int:
    result = estimator(_compact_json(payload))
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("TokenEstimator.estimate must return a non-negative integer")
    return result


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _payload(
    records: list[dict[str, Any]],
    references: list[Reference],
    *,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "evidence_format": EVIDENCE_FORMAT,
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": truncated,
        "data": {"results": records, "evidence_refs": references},
    }


def _index_references(
    references: list[Any],
) -> dict[tuple[str, str, str], Reference] | None:
    indexed: dict[tuple[str, str, str], Reference] = {}
    for value in references:
        if (
            not isinstance(value, Mapping)
            or not _REFERENCE_FIELDS <= set(value)
            or not all(
                isinstance(value[key], str) and value[key]
                for key in _REFERENCE_FIELDS
            )
        ):
            return None
        reference = dict(value)
        try:
            _compact_json(reference)
        except (TypeError, ValueError):
            return None
        match_id = (
            reference["chunk_id"],
            reference["document_revision_id"],
            reference["snapshot_id"],
        )
        if match_id in indexed:
            return None
        indexed[match_id] = reference
    return indexed


def _index_records(
    records: list[Any],
    references: Mapping[tuple[str, str, str], Reference],
) -> list[tuple[dict[str, Any], Reference]] | None:
    result: list[tuple[dict[str, Any], Reference]] = []
    seen: set[tuple[str, str, str]] = set()
    by_pair: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for match_id in references:
        by_pair.setdefault(match_id[:2], set()).add(match_id)
    for value in records:
        if not isinstance(value, Mapping):
            return None
        chunk_id = value.get("chunk_id")
        revision_id = value.get("document_revision_id")
        if (
            not isinstance(chunk_id, str)
            or not chunk_id
            or not isinstance(revision_id, str)
            or not revision_id
            or not isinstance(value.get("snippet"), str)
        ):
            return None
        snapshot_id = value.get("snapshot_id")
        if snapshot_id is not None and (
            not isinstance(snapshot_id, str) or not snapshot_id
        ):
            return None
        pair_matches = by_pair.get((chunk_id, revision_id), set())
        candidate_ids = {
            match_id
            for match_id in pair_matches
            if snapshot_id is None or match_id[2] == snapshot_id
        }
        if len(candidate_ids) != 1:
            return None
        match_id = next(iter(candidate_ids))
        if match_id in seen:
            return None
        record = dict(value)
        try:
            _compact_json(record)
        except (TypeError, ValueError):
            return None
        result.append((record, references[match_id]))
        seen.add(match_id)
    if set(references) != seen:
        return None
    return result


__all__ = [
    "EVIDENCE_FORMAT",
    "project_rag_evidence_payload",
    "reproject_rag_evidence_payload",
]
