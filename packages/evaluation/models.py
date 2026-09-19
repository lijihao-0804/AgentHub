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


__all__ = [
    "EvaluationDataset",
    "EvaluationDatasetCategory",
    "EvaluationDatasetItem",
    "EvaluationDatasetSplit",
    "EvaluationDatasetVersion",
    "EvaluationDatasetVersionStatus",
    "PricingSnapshot",
]
