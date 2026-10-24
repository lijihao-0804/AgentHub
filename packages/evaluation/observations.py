"""Versioned safe observation projections; evidence comes from execution, never labels."""

from __future__ import annotations

from typing import Any

from packages.knowledge.contracts import RetrievalQuery, RetrievalResult

OBSERVATION_SCHEMA_VERSION = 2
OBSERVATION_DRIVERS = frozenset({"runtime", "controlled", "synthetic"})


def observation_identity(driver: str, sources: dict[str, str]) -> dict[str, Any]:
    if driver not in OBSERVATION_DRIVERS:
        raise ValueError("INVALID_OBSERVATION_DRIVER")
    return {
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "driver_kind": driver,
        "field_sources": dict(sources),
    }


def retrieval_observation(
    result: RetrievalResult, query: RetrievalQuery, *, variant_hash: str
) -> dict[str, Any]:
    if result.trace.snapshot_id != query.knowledge_snapshot_id:
        raise ValueError("RETRIEVAL_SNAPSHOT_MISMATCH")
    candidate = [entry.chunk_id for entry in result.trace.fusion.results]
    final = [entry.chunk_id for entry in result.evidence]
    selection = result.trace.selection
    selection_ids = (
        [entry.chunk_id for entry in selection.results] if selection is not None else None
    )
    field_sources = {
        "candidate_chunk_ids": "retrieval_trace.fusion",
        "final_chunk_ids": "evidence",
    }
    stage_durations = {
        name: [getattr(result.trace, name).latency_ms]
        for name in ("dense", "sparse", "fusion", "rerank")
    }
    if selection is not None:
        field_sources["selection_chunk_ids"] = "retrieval_trace.selection"
        stage_durations["selection"] = [selection.latency_ms]
    context_selection = result.trace.rerank.metadata
    if context_selection is not None:
        field_sources["context_selection"] = "retrieval_trace.rerank.metadata"
    return {
        **observation_identity("runtime", field_sources),
        "category": "RETRIEVAL",
        "variant_hash": variant_hash,
        "snapshot_id": result.trace.snapshot_id,
        "retrieval_strategy": str(query.strategy),
        "candidate_top_k": query.candidate_top_k,
        "final_top_k": query.final_top_k,
        "rerank_top_k": query.rerank_top_k,
        "max_chunks_per_document": query.max_chunks_per_document,
        "context_budget_bytes": query.context_budget_bytes,
        "context_selection": context_selection,
        "candidate_chunk_ids": candidate,
        "selection_chunk_ids": selection_ids,
        "final_chunk_ids": final,
        # Compatibility for callers of the old projection; no candidate fallback in v2.
        "chunk_ids": final,
        "timing": {
            "clock": "process_monotonic",
            "origin": "retrieval_execution",
            "stage_durations_ms": stage_durations,
        },
    }
