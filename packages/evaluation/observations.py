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
    return {
        **observation_identity(
            "runtime",
            {"candidate_chunk_ids": "retrieval_trace.fusion", "final_chunk_ids": "evidence"},
        ),
        "category": "RETRIEVAL",
        "variant_hash": variant_hash,
        "snapshot_id": result.trace.snapshot_id,
        "retrieval_strategy": str(query.strategy),
        "candidate_top_k": query.candidate_top_k,
        "final_top_k": query.final_top_k,
        "candidate_chunk_ids": candidate,
        "final_chunk_ids": final,
        # Compatibility for callers of the old projection; no candidate fallback in v2.
        "chunk_ids": final,
        "timing": {
            "clock": "process_monotonic",
            "origin": "retrieval_execution",
            "stage_durations_ms": {
                name: [getattr(result.trace, name).latency_ms]
                for name in ("dense", "sparse", "fusion", "rerank")
            },
        },
    }
