from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: UUID | None = None
    client_key: str = Field(min_length=1, max_length=64)
    rating: Literal[-1, 1]
    category: Literal["FACTUAL", "RETRIEVAL", "TOOL", "LATENCY", "EXPRESSION", "OTHER"]
    comment: str = Field(default="", max_length=4000)
    corrected_answer: str | None = Field(default=None, max_length=16000)


class FeedbackReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    decision: Literal["APPROVED", "REJECTED"]
    comment: str = Field(default="", max_length=4000)


class FeedbackImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: UUID
    base_version_id: UUID
    expected_review_version: int = Field(ge=1)


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workspace_id: UUID
    run_id: UUID
    turn_id: UUID | None
    agent_version_id: UUID
    created_by: UUID
    schema_version: int
    content_revision: int
    rating: int
    category: str
    comment: str
    corrected_answer: str | None
    status: str
    review_version: int
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_comment: str | None
    imported_version_id: UUID | None
    created_at: datetime
