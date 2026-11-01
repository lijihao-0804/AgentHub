"""Immutable persistence records for protected Thread source reviews.

Rows in these tables contain source identities, hashes, eligibility revisions,
and exact character coordinates. They never duplicate Thread or excerpt text.
The migration that owns these tables also installs append-only triggers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base

SUMMARY_REVIEW_STORAGE_VERSION = "summary-review-store-v1"
SUMMARY_REVIEW_BASIS = "ADMIN_EXACT_EXCERPTS_V1"
SUMMARY_REVIEW_COMPLETENESS = "ATTESTED_NOT_PROVEN"
MAX_SUMMARY_REVIEW_SOURCES = 200
MAX_SUMMARY_REVIEW_EXCERPTS = 20_000


class SummaryReviewRecord(Base):
    """One immutable, explicitly versioned protected-source review."""

    __tablename__ = "summary_review_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_summary_review_records_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trigger_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_review_records_trigger_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_summary_review_records_reviewer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "revision",
            name="uq_summary_review_records_revision",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_summary_review_records_scope_id",
        ),
        PrimaryKeyConstraint("id", name="pk_summary_review_records"),
        CheckConstraint("revision > 0", name="ck_summary_review_records_revision"),
        CheckConstraint(
            "before_sequence > 0",
            name="ck_summary_review_records_before_sequence",
        ),
        CheckConstraint(
            f"source_count > 0 AND source_count <= {MAX_SUMMARY_REVIEW_SOURCES}",
            name="ck_summary_review_records_source_count",
        ),
        CheckConstraint(
            f"excerpt_count >= 0 AND excerpt_count <= {MAX_SUMMARY_REVIEW_EXCERPTS}",
            name="ck_summary_review_records_excerpt_count",
        ),
        CheckConstraint(
            "source_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND review_hash ~ '^[0-9a-f]{64}$'",
            name="ck_summary_review_records_hash_formats",
        ),
        CheckConstraint(
            f"review_basis = '{SUMMARY_REVIEW_BASIS}'",
            name="ck_summary_review_records_basis",
        ),
        CheckConstraint(
            f"contract_version = '{SUMMARY_REVIEW_STORAGE_VERSION}'",
            name="ck_summary_review_records_contract_version",
        ),
        CheckConstraint(
            f"completeness_status = '{SUMMARY_REVIEW_COMPLETENESS}'",
            name="ck_summary_review_records_completeness",
        ),
        CheckConstraint(
            "protected_manifest_attested = true "
            "AND reviewer_admin_verified = true",
            name="ck_summary_review_records_attestations",
        ),
        CheckConstraint(
            "octet_length(manifest_json::text) <= 4194304 "
            "AND octet_length(audit_json::text) <= 4194304",
            name="ck_summary_review_records_json_size",
        ),
        Index(
            "ix_summary_review_records_thread_revision",
            "workspace_id",
            "thread_id",
            "revision",
        ),
        Index(
            "ix_summary_review_records_reviewer",
            "reviewed_by",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    trigger_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    before_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excerpt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_basis: Mapped[str] = mapped_column(
        String(64), nullable=False, default=SUMMARY_REVIEW_BASIS
    )
    completeness_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SUMMARY_REVIEW_COMPLETENESS
    )
    contract_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default=SUMMARY_REVIEW_STORAGE_VERSION
    )
    protected_manifest_attested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    reviewer_admin_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reviewed_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SummaryReviewSource(Base):
    """A fixed reviewed source and the eligibility audit that authorized it."""

    __tablename__ = "summary_review_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id"],
            [
                "summary_review_records.workspace_id",
                "summary_review_records.thread_id",
                "summary_review_records.id",
            ],
            name="fk_summary_review_sources_record",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_summary_review_sources_turn",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_review_sources_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["eligibility_review_id"],
            ["thread_summary_eligibility_records.id"],
            name="fk_summary_review_sources_eligibility",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_summary_review_sources_reviewer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "ordinal",
            name="uq_summary_review_sources_ordinal",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "source_id",
            name="uq_summary_review_sources_source_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "source_identity",
            name="uq_summary_review_sources_identity",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "sequence",
            name="uq_summary_review_sources_sequence",
        ),
        PrimaryKeyConstraint("id", name="pk_summary_review_sources"),
        CheckConstraint(
            "ordinal > 0 AND sequence > 0 AND source_watermark > 0",
            name="ck_summary_review_sources_positive_order",
        ),
        CheckConstraint(
            "source_identity ~ '^[0-9a-f]{64}$' "
            "AND revision_sha256 ~ '^[0-9a-f]{64}$' "
            "AND user_input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND final_output_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_summary_review_sources_hash_formats",
        ),
        CheckConstraint(
            "review_decision = 'ELIGIBLE' AND review_reason = 'ADMIN_REVIEWED'",
            name="ck_summary_review_sources_decision",
        ),
        CheckConstraint(
            "eligibility_revision > 0",
            name="ck_summary_review_sources_eligibility_revision",
        ),
        CheckConstraint(
            "artifact_identities = '[]'::jsonb",
            name="ck_summary_review_sources_artifact_free",
        ),
        Index(
            "ix_summary_review_sources_turn",
            "workspace_id",
            "thread_id",
            "turn_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    review_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_watermark: Mapped[int] = mapped_column(Integer, nullable=False)
    original_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    final_output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_identities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    review_decision: Mapped[str] = mapped_column(String(16), nullable=False)
    review_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    eligibility_review_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    eligibility_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SummaryReviewExcerpt(Base):
    """Exact source coordinate and content digest; source text stays in Thread."""

    __tablename__ = "summary_review_excerpts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id"],
            [
                "summary_review_records.workspace_id",
                "summary_review_records.thread_id",
                "summary_review_records.id",
            ],
            name="fk_summary_review_excerpts_record",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id", "source_id"],
            [
                "summary_review_sources.workspace_id",
                "summary_review_sources.thread_id",
                "summary_review_sources.review_id",
                "summary_review_sources.source_id",
            ],
            name="fk_summary_review_excerpts_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "ordinal",
            name="uq_summary_review_excerpts_ordinal",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "fact_ref",
            name="uq_summary_review_excerpts_fact",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "source_id",
            "role",
            "start_offset",
            "end_offset",
            name="uq_summary_review_excerpts_coordinate",
        ),
        PrimaryKeyConstraint("id", name="pk_summary_review_excerpts"),
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_summary_review_excerpts_role",
        ),
        CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_summary_review_excerpts_offsets",
        ),
        CheckConstraint(
            "excerpt_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_summary_review_excerpts_hash",
        ),
        Index(
            "ix_summary_review_excerpts_source",
            "workspace_id",
            "thread_id",
            "review_id",
            "source_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    review_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    excerpt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SummaryReviewJobBinding(Base):
    """Fix one summary job to one persisted review revision."""

    __tablename__ = "summary_review_job_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_summary_review_job_bindings_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id"],
            [
                "summary_review_records.workspace_id",
                "summary_review_records.thread_id",
                "summary_review_records.id",
            ],
            name="fk_summary_review_job_bindings_review",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_summary_review_job_bindings_job",
        ),
        PrimaryKeyConstraint("id", name="pk_summary_review_job_bindings"),
        CheckConstraint(
            "review_revision > 0",
            name="ck_summary_review_job_bindings_revision",
        ),
        Index(
            "ix_summary_review_job_bindings_review",
            "workspace_id",
            "thread_id",
            "review_id",
            "review_revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    review_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "MAX_SUMMARY_REVIEW_EXCERPTS",
    "MAX_SUMMARY_REVIEW_SOURCES",
    "SUMMARY_REVIEW_BASIS",
    "SUMMARY_REVIEW_COMPLETENESS",
    "SUMMARY_REVIEW_STORAGE_VERSION",
    "SummaryReviewExcerpt",
    "SummaryReviewJobBinding",
    "SummaryReviewRecord",
    "SummaryReviewSource",
]
