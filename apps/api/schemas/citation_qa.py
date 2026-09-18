from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.api.schemas.knowledge import RetrievalPlaygroundStages


class CitationQaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    knowledge_base_id: UUID
    knowledge_snapshot_id: UUID
    model_profile_id: UUID
    query: str = Field(min_length=1, max_length=4_000)


class CitationQaCitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    document_id: UUID
    document_revision_id: UUID
    chunk_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    locator: dict[str, object]
    excerpt: str = Field(max_length=1_000)
    retrieval_score: float
    rerank_score: float | None


class CitationQaRetrievalTraceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    stages: RetrievalPlaygroundStages
    total_latency_ms: float


class CitationQaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[CitationQaCitationResponse]
    retrieval_trace: CitationQaRetrievalTraceResponse


__all__ = [
    "CitationQaCitationResponse",
    "CitationQaRequest",
    "CitationQaResponse",
    "CitationQaRetrievalTraceResponse",
]
