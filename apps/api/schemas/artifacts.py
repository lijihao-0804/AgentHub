"""Request and response contracts for artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_TITLE_LENGTH = 300


class ArtifactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    # Shape-checked by the per-type validator, not here: the table is generic,
    # the contents are not, and the whitelist lives in one place.
    content: dict[str, Any]


class ArtifactPatchRequest(BaseModel):
    """``type`` is absent on purpose — an artifact does not change kind."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)
    content: dict[str, Any] | None = None


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    thread_id: UUID
    # Set means the artifact is the record of one run and cannot be edited.
    run_id: UUID | None
    type: str
    title: str
    content: dict[str, Any]
    created_by: UUID
    created_at: datetime
    updated_at: datetime


__all__ = ["ArtifactCreateRequest", "ArtifactPatchRequest", "ArtifactResponse"]
