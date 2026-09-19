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


class EvaluationExperimentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)
    dataset_version_id: UUID
    split: str
    purpose: str
    repetitions: int = Field(default=1, ge=1, le=5)


class EvaluationExperimentVariantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    agent_version_id: UUID
    pricing_snapshot_id: UUID
    ordinal: int = Field(ge=0, lt=5)
    variant_metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationExperimentVariantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    experiment_id: UUID
    label: str
    agent_version_id: UUID
    resolved_spec_hash: str = Field(min_length=64, max_length=64)
    pricing_snapshot_id: UUID
    pricing_snapshot_hash: str = Field(min_length=64, max_length=64)
    effective_knowledge_snapshots: list[dict[str, str]]
    variant_metadata: dict[str, Any]
    variant_hash: str = Field(min_length=64, max_length=64)
    ordinal: int
    created_at: datetime


class EvaluationExperimentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    description: str | None
    dataset_version_id: UUID
    dataset_content_hash: str = Field(min_length=64, max_length=64)
    dataset_schema_version: int
    split: str
    purpose: str
    repetitions: int
    status: str
    build_sha: str = Field(min_length=7, max_length=64)
    evaluator_manifest: dict[str, Any]
    spec_json: dict[str, Any] | None
    spec_hash: str | None = Field(default=None, min_length=64, max_length=64)
    holdout_exposure_index: int | None = None
    holdout_exposure_count: int | None = Field(default=None, ge=0)
    created_by: UUID
    created_at: datetime


class EvaluationExperimentDetailResponse(EvaluationExperimentResponse):
    variants: list[EvaluationExperimentVariantResponse]


class EvaluationExperimentRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    experiment_id: UUID
    status: str
    git_commit: str = Field(min_length=7, max_length=64)
    dataset_version_id: UUID
    dataset_hash: str = Field(min_length=64, max_length=64)
    experiment_spec_hash: str = Field(min_length=64, max_length=64)
    split: str
    purpose: str
    repetitions: int
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    safe_failure_message: str | None
    holdout_exposure_index: int | None
    created_by: UUID
    created_at: datetime


class EvaluationExperimentRunProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    pending: int
    running: int
    completed: int
    failed: int
    cancelled: int
    progress: float


class EvaluationComparisonCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_variant_id: UUID
    candidate_variant_id: UUID


class EvaluationComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    experiment_run_id: UUID
    baseline_variant_id: UUID
    candidate_variant_id: UUID
    metric_snapshot_id: UUID | None
    metric_snapshot_hash: str | None
    baseline_variant_hash: str | None
    candidate_variant_hash: str | None
    evaluator_manifest_hash: str | None
    comparison_hash: str | None
    status: str
    evaluator_versions: dict[str, Any]
    metrics: dict[str, Any]
    missing_pairs: int
    paired_pairs: int
    created_by: UUID | None
    created_at: datetime


class EvaluationAblationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    comparison_id: UUID
    experiment_run_id: UUID
    baseline_variant_id: UUID
    candidate_variant_id: UUID
    factor: str
    changed_paths: list[str]
    baseline_factor_hash: str = Field(min_length=64, max_length=64)
    candidate_factor_hash: str = Field(min_length=64, max_length=64)
    analysis_hash: str = Field(min_length=64, max_length=64)
    created_by: UUID
    created_at: datetime


class EvaluationReleaseGatePolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)
    policy_json: dict[str, Any]


class EvaluationReleaseGatePolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    description: str | None
    policy_json: dict[str, Any]
    policy_hash: str = Field(min_length=64, max_length=64)
    created_by: UUID
    created_at: datetime


class EvaluationReleaseGateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: UUID


class EvaluationReleaseGateDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    workspace_id: UUID
    comparison_id: UUID
    policy_id: UUID
    status: str
    rule_results: list[dict[str, Any]]
    reasons: list[str]
    comparison_hash: str = Field(min_length=64, max_length=64)
    policy_hash: str = Field(min_length=64, max_length=64)
    decision_hash: str = Field(min_length=64, max_length=64)
    created_by: UUID
    created_at: datetime


__all__ = [
    "EvaluationDatasetCreateRequest",
    "EvaluationDatasetItemRequest",
    "EvaluationDatasetItemResponse",
    "EvaluationDatasetResponse",
    "EvaluationDatasetVersionCreateRequest",
    "EvaluationDatasetVersionDetailResponse",
    "EvaluationDatasetVersionResponse",
    "EvaluationExperimentCreateRequest",
    "EvaluationExperimentDetailResponse",
    "EvaluationExperimentResponse",
    "EvaluationExperimentRunResponse",
    "EvaluationExperimentRunProgressResponse",
    "EvaluationComparisonCreateRequest",
    "EvaluationComparisonResponse",
    "EvaluationAblationResponse",
    "EvaluationReleaseGatePolicyCreateRequest",
    "EvaluationReleaseGatePolicyResponse",
    "EvaluationReleaseGateCreateRequest",
    "EvaluationReleaseGateDecisionResponse",
    "EvaluationExperimentVariantCreateRequest",
    "EvaluationExperimentVariantResponse",
    "PricingSnapshotCreateRequest",
    "PricingSnapshotResponse",
]
