"""M7-A versioned evaluation dataset and pricing persistence models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class EvaluationDatasetVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class EvaluationDatasetSplit(StrEnum):
    DEV = "DEV"
    HOLDOUT = "HOLDOUT"


class EvaluationDatasetCategory(StrEnum):
    RETRIEVAL = "RETRIEVAL"
    KNOWLEDGE_QA = "KNOWLEDGE_QA"
    TOOL = "TOOL"
    NO_ANSWER = "NO_ANSWER"
    APPROVAL = "APPROVAL"
    MULTI_STEP = "MULTI_STEP"
    FAILURE = "FAILURE"


class EvaluationDataset(Base):
    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evaluation_datasets_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_datasets_created_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_evaluation_datasets_workspace_id"),
        Index("ix_evaluation_datasets_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationDatasetVersion(Base):
    __tablename__ = "evaluation_dataset_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["evaluation_datasets.workspace_id", "evaluation_datasets.id"],
            name="fk_evaluation_dataset_versions_dataset_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_dataset_versions_created_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_evaluation_dataset_versions_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "dataset_id",
            "version_number",
            name="uq_evaluation_dataset_versions_number",
        ),
        CheckConstraint(
            "version_number > 0", name="ck_evaluation_dataset_versions_number_positive"
        ),
        CheckConstraint(
            "schema_version > 0", name="ck_evaluation_dataset_versions_schema_positive"
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED')",
            name="ck_evaluation_dataset_versions_status",
        ),
        Index(
            "ix_evaluation_dataset_versions_dataset_status",
            "workspace_id",
            "dataset_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dataset_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=EvaluationDatasetVersionStatus.DRAFT
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationDatasetItem(Base):
    __tablename__ = "evaluation_dataset_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_dataset_items_version_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id",
            "dataset_version_id",
            "case_key",
            name="uq_evaluation_dataset_items_case_key",
        ),
        UniqueConstraint(
            "workspace_id",
            "dataset_version_id",
            "ordinal",
            name="uq_evaluation_dataset_items_ordinal",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_evaluation_dataset_items_workspace_id"),
        CheckConstraint(
            "split IN ('DEV', 'HOLDOUT')",
            name="ck_evaluation_dataset_items_split",
        ),
        CheckConstraint(
            "category IN ('RETRIEVAL', 'KNOWLEDGE_QA', 'TOOL', 'NO_ANSWER', 'APPROVAL', "
            "'MULTI_STEP', 'FAILURE')",
            name="ck_evaluation_dataset_items_category",
        ),
        CheckConstraint("ordinal >= 0", name="ck_evaluation_dataset_items_ordinal_nonnegative"),
        Index(
            "ix_evaluation_dataset_items_version_split",
            "workspace_id",
            "dataset_version_id",
            "split",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dataset_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    case_key: Mapped[str] = mapped_column(String(200), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    source_provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class PricingSnapshot(Base):
    __tablename__ = "pricing_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_pricing_snapshots_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_pricing_snapshots_created_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_pricing_snapshots_workspace_id"),
        Index("ix_pricing_snapshots_workspace_effective", "workspace_id", "effective_at"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    input_price_per_1m: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    output_price_per_1m: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cached_input_price_per_1m: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)


class EvaluationExperimentStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"


class EvaluationExperimentPurpose(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    HOLDOUT_VALIDATION = "HOLDOUT_VALIDATION"
    RELEASE_GATE = "RELEASE_GATE"


class EvaluationExperimentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class EvaluationCaseResultStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EvaluationExperiment(Base):
    __tablename__ = "evaluation_experiments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evaluation_experiments_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_experiments_dataset_version_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_experiments_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "split IN ('DEV', 'HOLDOUT')", name="ck_evaluation_experiments_split"
        ),
        CheckConstraint(
            "purpose IN ('DEVELOPMENT', 'HOLDOUT_VALIDATION', 'RELEASE_GATE')",
            name="ck_evaluation_experiments_purpose",
        ),
        CheckConstraint(
            "(purpose = 'DEVELOPMENT' AND split = 'DEV') OR "
            "(purpose IN ('HOLDOUT_VALIDATION', 'RELEASE_GATE') AND split = 'HOLDOUT')",
            name="ck_evaluation_experiments_purpose_split",
        ),
        CheckConstraint(
            "repetitions BETWEEN 1 AND 5", name="ck_evaluation_experiments_repetitions"
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'READY')", name="ck_evaluation_experiments_status"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_evaluation_experiments_workspace_id"),
        Index("ix_evaluation_experiments_workspace_created", "workspace_id", "created_at"),
        Index(
            "ix_evaluation_experiments_workspace_dataset",
            "workspace_id",
            "dataset_version_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    dataset_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dataset_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=EvaluationExperimentStatus.DRAFT
    )
    build_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    spec_hash: Mapped[str | None] = mapped_column(String(64))
    holdout_exposure_index: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationExperimentVariant(Base):
    __tablename__ = "evaluation_experiment_variants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["evaluation_experiments.workspace_id", "evaluation_experiments.id"],
            name="fk_evaluation_experiment_variants_experiment_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name="fk_evaluation_experiment_variants_agent_version_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pricing_snapshot_id"],
            ["pricing_snapshots.workspace_id", "pricing_snapshots.id"],
            name="fk_evaluation_experiment_variants_pricing_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "ordinal >= 0 AND ordinal < 5", name="ck_evaluation_experiment_variants_ordinal"
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_experiment_variants_workspace_id"
        ),
        UniqueConstraint(
            "workspace_id",
            "experiment_id",
            "label",
            name="uq_evaluation_experiment_variants_label",
        ),
        UniqueConstraint(
            "workspace_id",
            "experiment_id",
            "ordinal",
            name="uq_evaluation_experiment_variants_ordinal",
        ),
        Index(
            "ix_evaluation_experiment_variants_experiment_ordinal",
            "workspace_id",
            "experiment_id",
            "ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    resolved_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pricing_snapshot_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    pricing_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_knowledge_snapshots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    variant_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    variant_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationExperimentRun(Base):
    __tablename__ = "evaluation_experiment_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["evaluation_experiments.workspace_id", "evaluation_experiments.id"],
            name="fk_evaluation_experiment_runs_experiment_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_experiment_runs_dataset_version_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_experiment_runs_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCEL_REQUESTED', "
            "'CANCELLED')",
            name="ck_evaluation_experiment_runs_status",
        ),
        CheckConstraint("split IN ('DEV', 'HOLDOUT')", name="ck_evaluation_experiment_runs_split"),
        CheckConstraint(
            "purpose IN ('DEVELOPMENT', 'HOLDOUT_VALIDATION', 'RELEASE_GATE')",
            name="ck_evaluation_experiment_runs_purpose",
        ),
        CheckConstraint(
            "repetitions BETWEEN 1 AND 5", name="ck_evaluation_experiment_runs_repetitions"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_evaluation_experiment_runs_workspace_id"),
        Index(
            "ix_evaluation_experiment_runs_experiment_created",
            "workspace_id",
            "experiment_id",
            "created_at",
        ),
        Index(
            "ix_evaluation_experiment_runs_workspace_status",
            "workspace_id",
            "status",
        ),
        Index(
            "ix_evaluation_experiment_runs_lease",
            "status",
            "lease_expires_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=EvaluationExperimentRunStatus.QUEUED
    )
    git_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))
    safe_failure_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationExperimentCaseResult(Base):
    __tablename__ = "evaluation_experiment_case_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_case_results_run_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_case_results_variant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "dataset_item_id"],
            ["evaluation_dataset_items.workspace_id", "evaluation_dataset_items.id"],
            name="fk_evaluation_case_results_dataset_item_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_evaluation_case_results_agent_run_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_evaluation_case_results_status",
        ),
        CheckConstraint("repetition_index >= 0", name="ck_evaluation_case_results_repetition"),
        UniqueConstraint(
            "workspace_id",
            "experiment_run_id",
            "experiment_variant_id",
            "dataset_item_id",
            "repetition_index",
            name="uq_evaluation_case_results_execution_key",
        ),
        UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_case_results_workspace_id"
        ),
        Index(
            "ix_evaluation_case_results_run_status",
            "workspace_id",
            "experiment_run_id",
            "status",
        ),
        Index(
            "ix_evaluation_case_results_run_order",
            "workspace_id",
            "experiment_run_id",
            "dataset_item_id",
            "experiment_variant_id",
            "repetition_index",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_variant_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dataset_item_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    repetition_index: Mapped[int] = mapped_column(Integer, nullable=False)
    case_execution_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=EvaluationCaseResultStatus.PENDING
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    failure_code: Mapped[str | None] = mapped_column(String(96))
    safe_failure_message: Mapped[str | None] = mapped_column(Text)
    observation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationExperimentHoldoutExposure(Base):
    __tablename__ = "evaluation_experiment_holdout_exposures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_holdout_exposures_dataset_version_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["evaluation_experiments.workspace_id", "evaluation_experiments.id"],
            name="fk_evaluation_holdout_exposures_experiment_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_holdout_exposures_run_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_holdout_exposures_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("exposure_index > 0", name="ck_evaluation_holdout_exposures_index"),
        UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_holdout_exposures_workspace_id"
        ),
        UniqueConstraint(
            "workspace_id",
            "dataset_version_id",
            "exposure_index",
            name="uq_evaluation_holdout_exposures_dataset_index",
        ),
        Index(
            "ix_evaluation_holdout_exposures_dataset_created",
            "workspace_id",
            "dataset_version_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dataset_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_run_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    exposure_index: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "EvaluationDataset",
    "EvaluationDatasetCategory",
    "EvaluationDatasetItem",
    "EvaluationDatasetSplit",
    "EvaluationDatasetVersion",
    "EvaluationDatasetVersionStatus",
    "EvaluationExperiment",
    "EvaluationCaseResultStatus",
    "EvaluationExperimentCaseResult",
    "EvaluationExperimentHoldoutExposure",
    "EvaluationExperimentPurpose",
    "EvaluationExperimentRun",
    "EvaluationExperimentRunStatus",
    "EvaluationExperimentStatus",
    "EvaluationExperimentVariant",
    "PricingSnapshot",
]
