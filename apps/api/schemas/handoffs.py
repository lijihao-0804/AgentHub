from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


class AssignRequest(VersionRequest):
    assignee_id: UUID


class CloseRequest(VersionRequest):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    unresolved_items: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    ] = Field(default_factory=list, max_length=50)


class HandoffResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    workspace_id: UUID
    thread_id: UUID
    source_artifact_id: UUID
    source_hash: str
    status: Literal["OPEN", "ASSIGNED", "IN_PROGRESS", "CLOSED"]
    version: int
    created_by: UUID
    assignee_id: UUID | None
    claimed_by: UUID | None
    closed_by: UUID | None
    closure_reason: str | None
    unresolved_items: list[str]
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None
    closed_at: datetime | None


class AssigneeResponse(BaseModel):
    user_id: UUID
    email: str
