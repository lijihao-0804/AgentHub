"""Stable identifiers for knowledge index points."""

from __future__ import annotations

from uuid import UUID, uuid5

AGENTHUB_KNOWLEDGE_NAMESPACE = UUID("7d1fef9e-0b3c-4b72-a8f2-6e0c6bda4f5b")


def deterministic_point_id(chunk_id: str) -> str:
    """Return the same Qdrant-safe UUID for every occurrence of a chunk_id."""

    return str(uuid5(AGENTHUB_KNOWLEDGE_NAMESPACE, chunk_id))


__all__ = ["AGENTHUB_KNOWLEDGE_NAMESPACE", "deterministic_point_id"]
