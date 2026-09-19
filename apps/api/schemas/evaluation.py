from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EvaluationDatasetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)


class EvaluationDatasetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    description: str | None
    created_by: UUID
    created_at: datetime


class EvaluationDatasetItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_key: str = Field(min_length=1, max_length=200)
    split: str
    category: str
    input: dict[str, Any]
    expected: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    source_provenance: dict[str, Any]
    ordinal: int = Field(ge=0)


class EvaluationDatasetItemResponse(EvaluationDatasetItemRequest):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    dataset_version_id: UUID


class EvaluationDatasetVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    items: list[EvaluationDatasetItemRequest] = Field(min_length=1)


class EvaluationDatasetVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    dataset_id: UUID
    version_number: int
    schema_version: int
    content_hash: str = Field(min_length=64, max_length=64)
    status: str
    created_by: UUID
    created_at: datetime
    published_at: datetime | None
    item_count: int | None = Field(default=None, ge=0)


class EvaluationDatasetVersionDetailResponse(EvaluationDatasetVersionResponse):
    items: list[EvaluationDatasetItemResponse]


class PricingSnapshotCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    currency: str = Field(min_length=3, max_length=3)
    input_price_per_1m: Decimal = Field(ge=0)
    output_price_per_1m: Decimal = Field(ge=0)
    cached_input_price_per_1m: Decimal | None = Field(default=None, ge=0)
    effective_at: datetime
    source_note: str = Field(min_length=1, max_length=4_000)


class PricingSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    provider: str
    model: str
    currency: str
    input_price_per_1m: Decimal
    output_price_per_1m: Decimal
    cached_input_price_per_1m: Decimal | None
    effective_at: datetime
    source_note: str
    content_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime
    created_by: UUID


__all__ = [
    "EvaluationDatasetCreateRequest",
    "EvaluationDatasetItemRequest",
    "EvaluationDatasetItemResponse",
    "EvaluationDatasetResponse",
    "EvaluationDatasetVersionCreateRequest",
    "EvaluationDatasetVersionDetailResponse",
    "EvaluationDatasetVersionResponse",
    "PricingSnapshotCreateRequest",
    "PricingSnapshotResponse",
]
