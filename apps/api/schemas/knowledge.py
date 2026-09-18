from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
