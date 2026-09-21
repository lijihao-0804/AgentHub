from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeBaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class KnowledgeBaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    name: str
    created_at: datetime


class DocumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    name: str
    created_at: datetime
    effective_date: date | None = None
    superseded_by_document_id: UUID | None = None


class DocumentLifecycleUpdateRequest(BaseModel):
    """Both fields are omissible, and ``None`` means "clear", not "unchanged".

    ``model_fields_set`` is what separates the two, so the route reads it
    rather than testing the values.
    """

    model_config = ConfigDict(extra="forbid")

    effective_date: date | None = None
    superseded_by_document_id: UUID | None = None

    @model_validator(mode="after")
    def validate_not_empty(self) -> DocumentLifecycleUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one lifecycle field must be provided")
        return self


class DocumentRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    document_id: UUID
    revision_number: int
    original_filename: str
    media_type: str
    file_size: int
    lifecycle_status: str
    ingestion_status: str
    created_at: datetime


class IngestionJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    document_revision_id: UUID
    status: str
    stage: str
    attempt_count: int
    last_error_code: str | None
    safe_error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: DocumentResponse
    revision: DocumentRevisionResponse
    ingestion_job: IngestionJobResponse


class DocumentRevisionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: DocumentRevisionResponse
    ingestion_job: IngestionJobResponse


class KnowledgeSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    content_hash: str = Field(min_length=64, max_length=64)
    snapshot_schema_version: int = Field(ge=1)
    item_count: int = Field(ge=0)
    created_at: datetime


class RetrievalPlaygroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    knowledge_snapshot_id: UUID
    dense_top_k: int = Field(default=30, ge=1, le=100)
    sparse_top_k: int = Field(default=30, ge=1, le=100)
    candidate_top_k: int = Field(default=20, ge=1, le=100)
    final_top_k: int = Field(default=6, ge=1, le=20)
    # The playground is where the floor gets calibrated, so it is the one
    # caller allowed to override it. Omitting the field keeps the configured
    # value; it does not mean "no floor".
    min_rerank_score: float | None = Field(default=None, ge=-100, le=100)

    @model_validator(mode="after")
    def validate_final_limit(self) -> RetrievalPlaygroundRequest:
        if self.final_top_k > self.candidate_top_k:
            raise ValueError("final_top_k must be less than or equal to candidate_top_k")
        return self


class RetrievalPlaygroundStageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    rank: int
    score: float
    document_revision_id: str | None = None
    locator: dict[str, object] | None = None


class RetrievalPlaygroundStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency_ms: float
    results: list[RetrievalPlaygroundStageResult]


class RetrievalPlaygroundStages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dense: RetrievalPlaygroundStage
    sparse: RetrievalPlaygroundStage
    fused: RetrievalPlaygroundStage
    rerank: RetrievalPlaygroundStage


class RetrievalPlaygroundEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    document_revision_id: str
    source: str
    locator: dict[str, object]
    retrieval_score: float
    rerank_score: float | None
    snippet: str
    # The playground is where a human calibrates knowledge_min_rerank_score and
    # decides whether a retired document is out-ranking its successor, so it
    # shows the lifecycle the ranking now depends on. Unlike the search_knowledge
    # tool result this keeps the successor's id: a raw UUID is noise to a model,
    # but it is the thing a person needs in order to go look at that document.
    document_name: str | None = None
    effective_date: str | None = None
    superseded: bool = False
    superseded_by_document_id: str | None = None


class RetrievalPlaygroundResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    evidence: list[RetrievalPlaygroundEvidence]
    stages: RetrievalPlaygroundStages
    total_latency_ms: float
