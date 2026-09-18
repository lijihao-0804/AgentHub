from __future__ import annotations

from dataclasses import dataclass

from apps.api.schemas.knowledge import (
    RetrievalPlaygroundStage,
    RetrievalPlaygroundStageResult,
    RetrievalPlaygroundStages,
)
from packages.knowledge.contracts import RetrievalTrace, RetrievalTraceStage


@dataclass(frozen=True, slots=True)
class RetrievalTraceMetadata:
    document_revision_id: str
    locator: dict[str, object]


def project_retrieval_stage(
    stage: RetrievalTraceStage,
    metadata: dict[str, RetrievalTraceMetadata] | None = None,
) -> RetrievalPlaygroundStage:
    metadata = metadata or {}
    return RetrievalPlaygroundStage(
        latency_ms=stage.latency_ms,
        results=[
            RetrievalPlaygroundStageResult(
                chunk_id=item.chunk_id,
                rank=item.rank,
                score=item.score,
                document_revision_id=(
                    metadata[item.chunk_id].document_revision_id
                    if item.chunk_id in metadata
                    else None
                ),
                locator=metadata[item.chunk_id].locator if item.chunk_id in metadata else None,
            )
            for item in stage.results
        ],
    )


def project_retrieval_trace(
    trace: RetrievalTrace,
    metadata: dict[str, RetrievalTraceMetadata] | None = None,
) -> RetrievalPlaygroundStages:
    return RetrievalPlaygroundStages(
        dense=project_retrieval_stage(trace.dense, metadata),
        sparse=project_retrieval_stage(trace.sparse, metadata),
        fused=project_retrieval_stage(trace.fusion, metadata),
        rerank=project_retrieval_stage(trace.rerank, metadata),
    )


__all__ = ["RetrievalTraceMetadata", "project_retrieval_stage", "project_retrieval_trace"]
