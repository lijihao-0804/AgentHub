"""Shared source-record projection before model-facing evidence assembly."""

from __future__ import annotations

from typing import Any

from packages.knowledge.contracts import RetrievedEvidence

MAX_EVIDENCE_EXCERPT_CHARS = 1_000


def evidence_record(item: RetrievedEvidence, *, snapshot_id: str | None = None) -> dict[str, Any]:
    record = {
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "document_revision_id": item.document_revision_id,
        "source": item.source,
        "locator": dict(item.locator),
        "snippet": item.text[:MAX_EVIDENCE_EXCERPT_CHARS],
        "retrieval_score": item.retrieval_score,
        "rerank_score": item.rerank_score,
        "document_name": item.metadata.get("document_name"),
        "effective_date": item.metadata.get("effective_date"),
        "superseded": bool(item.metadata.get("superseded", False)),
    }
    if snapshot_id is not None:
        record["snapshot_id"] = snapshot_id
    return record


def evidence_reference(item: RetrievedEvidence, *, snapshot_id: str, knowledge_base_id: str):
    return {
        "snapshot_id": snapshot_id,
        "knowledge_base_id": knowledge_base_id,
        "document_revision_id": item.document_revision_id,
        "chunk_id": item.chunk_id,
    }
