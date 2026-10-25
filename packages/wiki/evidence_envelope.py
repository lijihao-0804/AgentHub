"""Unified evidence envelope for Wiki-composed deliveries.

Every item that enters a model through the Wiki path carries a judgeable
origin layer. ``RAW`` items are original snapshot chunks and keep their
existing record shape; ``WIKI_DERIVED`` items additionally carry the
publication identity they were compiled into, the Wiki page that holds them,
the original source revisions the compilation drew from, and the original
quote. ``publication_hash`` is registry-only: it travels in the private
checkpoint registry beside the record, never in a model-visible payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.contracts import EvidenceOrigin

EVIDENCE_ORIGIN_RAW = EvidenceOrigin.RAW.value
EVIDENCE_ORIGIN_WIKI_DERIVED = EvidenceOrigin.WIKI_DERIVED.value

WIKI_DERIVED_REQUIRED_FIELDS = (
    "origin",
    "publication_id",
    "publication_hash",
    "wiki_page_id",
    "source_revisions",
    "quote",
)

_WIKI_DERIVED_TEXT_FIELDS = ("publication_id", "publication_hash", "wiki_page_id", "quote")


def envelope_violation(record: Mapping[str, Any]) -> str | None:
    """Return the envelope violation reason, or ``None`` when the record is valid.

    RAW records are valid whenever their origin marker is absent or ``RAW``:
    the raw shape is owned by the existing retrieval records and is not
    re-validated here. A record claiming ``WIKI_DERIVED`` must carry the full
    wiki envelope; anything less fails closed at the registration boundary.
    """

    origin = record.get("origin")
    if origin is None or origin == EVIDENCE_ORIGIN_RAW:
        return None
    if origin != EVIDENCE_ORIGIN_WIKI_DERIVED:
        return f"unknown origin {origin!r}"
    missing = [key for key in WIKI_DERIVED_REQUIRED_FIELDS if key not in record]
    if missing:
        return f"missing fields {missing}"
    for key in _WIKI_DERIVED_TEXT_FIELDS:
        value = record[key]
        if not isinstance(value, str) or not value.strip():
            return f"field {key!r} must be a non-empty string"
    revisions = record["source_revisions"]
    if (
        not isinstance(revisions, list)
        or not revisions
        or any(not isinstance(item, str) or not item.strip() for item in revisions)
    ):
        return "field 'source_revisions' must be a non-empty list of revision ids"
    return None


def validate_wiki_derived_record(record: Mapping[str, Any]) -> None:
    """Fail closed on an incomplete WIKI_DERIVED record."""

    reason = envelope_violation(record)
    if reason is not None:
        raise AgentHubError(
            "WIKI_ENVELOPE_INVALID",
            "A Wiki-derived evidence record is missing its envelope identity.",
            500,
        ) from None


def stamp_raw_evidence(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy raw records with an explicit ``RAW`` origin marker.

    Existing fields are carried unchanged; stamping never mutates its input.
    """

    stamped: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        if item.get("origin") not in (None, EVIDENCE_ORIGIN_RAW):
            raise AgentHubError(
                "WIKI_ENVELOPE_INVALID",
                "A raw evidence record already carries a different origin.",
                500,
            )
        item["origin"] = EVIDENCE_ORIGIN_RAW
        stamped.append(item)
    return stamped


def stamp_wiki_derived_evidence(
    records: Sequence[Mapping[str, Any]],
    *,
    publication_id: str,
    publication_hash: str,
) -> list[dict[str, Any]]:
    """Copy wiki claims with their full WIKI_DERIVED envelope.

    ``wiki_page_id`` is the record's ``page_key``. ``source_revisions`` is the
    compiled page's original source revision list: the sorted, deduplicated
    revision ids of the page's accepted claims, so one item can always be
    traced back to the raw revisions behind its page. Missing publication
    identity fails closed instead of stamping a traceable-looking but
    unverifiable record.
    """

    if not isinstance(publication_id, str) or not publication_id.strip():
        raise AgentHubError(
            "WIKI_ENVELOPE_INVALID",
            "A Wiki publication id is required to stamp derived evidence.",
            500,
        )
    if not isinstance(publication_hash, str) or not publication_hash.strip():
        raise AgentHubError(
            "WIKI_ENVELOPE_INVALID",
            "A Wiki publication hash is required to stamp derived evidence.",
            500,
        )
    revisions_by_page: dict[str, list[str]] = {}
    for record in records:
        page_id = record.get("page_key")
        revision_id = record.get("document_revision_id")
        if not isinstance(page_id, str) or not page_id.strip():
            raise AgentHubError(
                "WIKI_ENVELOPE_INVALID",
                "A Wiki-derived evidence record has no page identity.",
                500,
            )
        if not isinstance(revision_id, str) or not revision_id.strip():
            raise AgentHubError(
                "WIKI_ENVELOPE_INVALID",
                "A Wiki-derived evidence record has no source revision.",
                500,
            )
        revisions = revisions_by_page.setdefault(page_id, [])
        if revision_id not in revisions:
            revisions.append(revision_id)
    for revisions in revisions_by_page.values():
        revisions.sort()

    stamped: list[dict[str, Any]] = []
    for record in records:
        if record.get("origin") not in (None, EVIDENCE_ORIGIN_WIKI_DERIVED):
            raise AgentHubError(
                "WIKI_ENVELOPE_INVALID",
                "A Wiki claim already carries a different origin.",
                500,
            )
        page_id = str(record["page_key"])
        item = dict(record)
        item["origin"] = EVIDENCE_ORIGIN_WIKI_DERIVED
        item["publication_id"] = publication_id
        item["publication_hash"] = publication_hash
        item["wiki_page_id"] = page_id
        item["source_revisions"] = list(revisions_by_page[page_id])
        validate_wiki_derived_record(item)
        stamped.append(item)
    return stamped


def wiki_evidence_reference(
    record: Mapping[str, Any],
    *,
    snapshot_id: str,
    knowledge_base_id: str,
) -> dict[str, str]:
    """The private registry reference for one envelope-stamped record."""

    return {
        "snapshot_id": str(snapshot_id),
        "knowledge_base_id": str(knowledge_base_id),
        "document_revision_id": str(record["document_revision_id"]),
        "chunk_id": str(record["chunk_id"]),
    }


__all__ = [
    "EVIDENCE_ORIGIN_RAW",
    "EVIDENCE_ORIGIN_WIKI_DERIVED",
    "WIKI_DERIVED_REQUIRED_FIELDS",
    "envelope_violation",
    "stamp_raw_evidence",
    "stamp_wiki_derived_evidence",
    "validate_wiki_derived_record",
    "wiki_evidence_reference",
]
