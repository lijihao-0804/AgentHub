"""Durable thread-summary persistence contracts.

The source manifest keeps identities and hashes, never duplicated Thread text.
Provider output is separate from the job so a crash cannot erase the fact that
a dispatch happened or a response arrived.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.agent_runtime.summary_state import SummaryJobStatus
from packages.core.database import Base

_JOB_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in SummaryJobStatus)
_JOB_STATUS_SQL = f"status IN ({_JOB_STATUS_VALUES})"
_RESERVED_JOB_STATUSES = (
    "'DISPATCH_INTENT', 'RESPONSE_STORED', 'VALIDATED', "
    "'PUBLISHED', 'STALE', 'UNKNOWN_OUTCOME'"
)
_RESPONSE_JOB_STATUSES = "'RESPONSE_STORED', 'VALIDATED', 'PUBLISHED', 'STALE'"
_DISPATCHED_JOB_STATUSES = (
    "'DISPATCH_INTENT', 'RESPONSE_STORED', 'VALIDATED', "
    "'PUBLISHED', 'STALE', 'UNKNOWN_OUTCOME'"
)


class ThreadSummaryDispatchOutcome(StrEnum):
    """Append-only outcomes for the one provider dispatch a job may make."""

    INTENT_RECORDED = "INTENT_RECORDED"
    DISPATCHED = "DISPATCHED"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    FAILED_NOT_DISPATCHED = "FAILED_NOT_DISPATCHED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class ThreadSummaryArtifactStatus(StrEnum):
    """Publication state for an immutable, validated summary candidate."""

    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    STALE = "STALE"
    FAILED = "FAILED"


class ThreadSummaryJob(Base):
    """One deduplicated summary request for a Thread and triggering Run."""

    __tablename__ = "thread_summary_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_thread_summary_jobs_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_thread_summary_jobs_thread_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trigger_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_thread_summary_jobs_run_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_thread_summary_jobs_created_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "parent_artifact_id"],
            [
                "thread_summary_artifacts.workspace_id",
                "thread_summary_artifacts.thread_id",
                "thread_summary_artifacts.id",
            ],
            name="fk_thread_summary_jobs_parent_artifact_workspace",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "id", "dispatch_id"],
            [
                "thread_summary_dispatches.workspace_id",
                "thread_summary_dispatches.thread_id",
                "thread_summary_dispatches.job_id",
                "thread_summary_dispatches.id",
            ],
            name="fk_thread_summary_jobs_dispatch_scope",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "id", "response_id"],
            [
                "thread_summary_responses.workspace_id",
                "thread_summary_responses.thread_id",
                "thread_summary_responses.job_id",
                "thread_summary_responses.id",
            ],
            name="fk_thread_summary_jobs_response_scope",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "id", "artifact_id"],
            [
                "thread_summary_artifacts.workspace_id",
                "thread_summary_artifacts.thread_id",
                "thread_summary_artifacts.job_id",
                "thread_summary_artifacts.id",
            ],
            name="fk_thread_summary_jobs_artifact_scope",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_jobs_workspace_thread_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "identity_hash",
            name="uq_thread_summary_jobs_identity",
        ),
        UniqueConstraint(
            "workspace_id",
            "trigger_run_id",
            "model_round",
            name="uq_thread_summary_jobs_dispatch_boundary",
        ),
        UniqueConstraint(
            "workspace_id",
            "trigger_run_id",
            "checkpoint_id",
            "checkpoint_ns",
            name="uq_thread_summary_jobs_exact_checkpoint",
        ),
        CheckConstraint(_JOB_STATUS_SQL, name="ck_thread_summary_jobs_status"),
        CheckConstraint(
            "model_round IS NULL OR "
            "(model_round > 0 AND model_round <= 2147483647)",
            name="ck_thread_summary_jobs_model_round_bounds",
        ),
        CheckConstraint(
            "(checkpoint_id IS NULL AND checkpoint_ns IS NULL "
            "AND checkpoint_model_round_count IS NULL "
            "AND checkpoint_binding_sha256 IS NULL) OR "
            "(checkpoint_id IS NOT NULL AND checkpoint_ns IS NOT NULL "
            "AND checkpoint_model_round_count IS NOT NULL "
            "AND checkpoint_binding_sha256 IS NOT NULL)",
            name="ck_thread_summary_jobs_checkpoint_shape",
        ),
        CheckConstraint(
            "checkpoint_id IS NULL OR length(checkpoint_id) > 0",
            name="ck_thread_summary_jobs_checkpoint_id_nonempty",
        ),
        CheckConstraint(
            "(checkpoint_id IS NULL OR octet_length(checkpoint_id) <= 1024) "
            "AND (checkpoint_ns IS NULL OR octet_length(checkpoint_ns) <= 1024)",
            name="ck_thread_summary_jobs_checkpoint_selector_size",
        ),
        CheckConstraint(
            "checkpoint_model_round_count IS NULL OR "
            "(checkpoint_model_round_count >= 0 "
            "AND checkpoint_model_round_count <= 2147483647)",
            name="ck_thread_summary_jobs_checkpoint_round_bounds",
        ),
        CheckConstraint(
            "model_round IS NULL OR checkpoint_id IS NULL OR "
            "checkpoint_model_round_count = model_round - 1",
            name="ck_thread_summary_jobs_checkpoint_round_relation",
        ),
        CheckConstraint(
            "checkpoint_binding_sha256 IS NULL OR "
            "checkpoint_binding_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_jobs_checkpoint_hash",
        ),
        CheckConstraint(
            "source_watermark > 0",
            name="ck_thread_summary_jobs_watermark_positive",
        ),
        CheckConstraint(
            "target_budget_tokens > 0",
            name="ck_thread_summary_jobs_target_budget_positive",
        ),
        CheckConstraint(
            "octet_length(source_scope::text) <= 1048576",
            name="ck_thread_summary_jobs_source_scope_size",
        ),
        CheckConstraint(
            "octet_length(policy_spec::text) <= 262144",
            name="ck_thread_summary_jobs_policy_spec_size",
        ),
        CheckConstraint(
            "octet_length(model_spec::text) <= 262144",
            name="ck_thread_summary_jobs_model_spec_size",
        ),
        CheckConstraint(
            "identity_hash ~ '^[0-9a-f]{64}$' "
            "AND source_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND source_content_hash ~ '^[0-9a-f]{64}$' "
            "AND model_spec_hash ~ '^[0-9a-f]{64}$' "
            "AND permission_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_jobs_hash_formats",
        ),
        CheckConstraint(
            "(parent_artifact_id IS NULL AND parent_revision_hash IS NULL) OR "
            "(parent_artifact_id IS NOT NULL AND parent_revision_hash IS NOT NULL AND "
            "parent_revision_hash ~ '^[0-9a-f]{64}$')",
            name="ck_thread_summary_jobs_parent",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_thread_summary_jobs_lease_pair",
        ),
        CheckConstraint(
            "lease_token IS NULL OR length(lease_token) > 0",
            name="ck_thread_summary_jobs_lease_nonempty",
        ),
        CheckConstraint(
            "status <> 'QUEUED' OR lease_token IS NULL",
            name="ck_thread_summary_jobs_queued_without_lease",
        ),
        CheckConstraint(
            "status NOT IN ('CLAIMED', 'DISPATCH_INTENT') OR lease_token IS NOT NULL",
            name="ck_thread_summary_jobs_active_lease",
        ),
        CheckConstraint(
            "status <> 'UNKNOWN_OUTCOME' OR lease_token IS NULL",
            name="ck_thread_summary_jobs_unknown_without_lease",
        ),
        CheckConstraint(
            "(status NOT IN ('QUEUED', 'CLAIMED')) OR budget_reserved = false",
            name="ck_thread_summary_jobs_pre_dispatch_budget",
        ),
        CheckConstraint(
            f"(status NOT IN ({_RESERVED_JOB_STATUSES})) OR budget_reserved = true",
            name="ck_thread_summary_jobs_reserved_budget",
        ),
        CheckConstraint(
            f"(status NOT IN ({_DISPATCHED_JOB_STATUSES})) OR dispatch_id IS NOT NULL",
            name="ck_thread_summary_jobs_dispatch_pointer",
        ),
        CheckConstraint(
            f"(status NOT IN ({_RESPONSE_JOB_STATUSES})) OR response_id IS NOT NULL",
            name="ck_thread_summary_jobs_response_pointer",
        ),
        CheckConstraint(
            "status <> 'PUBLISHED' OR artifact_id IS NOT NULL",
            name="ck_thread_summary_jobs_published_pointer",
        ),
        Index(
            "ix_thread_summary_jobs_thread_status",
            "workspace_id",
            "thread_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_thread_summary_jobs_lease",
            "status",
            "lease_expires_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    trigger_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    model_round: Mapped[int | None] = mapped_column(Integer)
    checkpoint_id: Mapped[str | None] = mapped_column(Text)
    checkpoint_ns: Mapped[str | None] = mapped_column(Text)
    checkpoint_model_round_count: Mapped[int | None] = mapped_column(Integer)
    checkpoint_binding_sha256: Mapped[str | None] = mapped_column(String(64))
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_watermark: Mapped[int] = mapped_column(Integer, nullable=False)
    source_scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_artifact_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    parent_revision_hash: Mapped[str | None] = mapped_column(String(64))
    policy_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tokenizer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tokenizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    permission_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=SummaryJobStatus.QUEUED.value,
        server_default=SummaryJobStatus.QUEUED.value,
    )
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_reserved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    dispatch_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    response_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    artifact_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ThreadSummaryJobSource(Base):
    """A source coordinate and hashes only; the original Thread text stays in Turn."""

    __tablename__ = "thread_summary_job_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_job_sources_job_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_thread_summary_job_sources_turn_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_thread_summary_job_sources_run_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "job_id",
            "ordinal",
            name="uq_thread_summary_job_sources_ordinal",
        ),
        UniqueConstraint(
            "workspace_id",
            "job_id",
            "source_id",
            name="uq_thread_summary_job_sources_source_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "job_id",
            "sequence",
            name="uq_thread_summary_job_sources_sequence",
        ),
        CheckConstraint(
            "ordinal > 0 AND sequence > 0",
            name="ck_thread_summary_job_sources_positive_order",
        ),
        CheckConstraint(
            "source_identity_hash ~ '^[0-9a-f]{64}$' "
            "AND revision_sha256 ~ '^[0-9a-f]{64}$' "
            "AND user_input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND final_output_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_job_sources_hash_formats",
        ),
        CheckConstraint(
            "summary_eligibility IN ('ELIGIBLE', 'INELIGIBLE', 'UNKNOWN')",
            name="ck_thread_summary_job_sources_eligibility",
        ),
        CheckConstraint(
            "octet_length(artifact_identities::text) <= 1048576",
            name="ck_thread_summary_job_sources_artifacts_size",
        ),
        Index(
            "ix_thread_summary_job_sources_run",
            "workspace_id",
            "thread_id",
            "run_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    original_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    source_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    final_output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_identities: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    summary_eligibility: Mapped[str] = mapped_column(String(16), nullable=False)
    eligibility_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadSummaryDispatch(Base):
    """One durable dispatch intent, budget reservation and outcome ledger row."""

    __tablename__ = "thread_summary_dispatches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_dispatches_job_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pricing_snapshot_id"],
            ["pricing_snapshots.workspace_id", "pricing_snapshots.id"],
            name="fk_thread_summary_dispatches_pricing_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_dispatches_workspace_thread_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "id",
            name="uq_thread_summary_dispatches_workspace_thread_job_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_thread_summary_dispatches_job",
        ),
        UniqueConstraint("reservation_id", name="uq_thread_summary_dispatches_reservation"),
        CheckConstraint("attempt_number = 1", name="ck_thread_summary_dispatches_single_attempt"),
        CheckConstraint(
            "outcome IN ('INTENT_RECORDED', 'DISPATCHED', 'RESPONSE_RECEIVED', "
            "'FAILED_NOT_DISPATCHED', 'UNKNOWN_OUTCOME')",
            name="ck_thread_summary_dispatches_outcome",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' "
            "AND intent_hash ~ '^[0-9a-f]{64}$' "
            "AND budget_reservation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_dispatches_hash_formats",
        ),
        CheckConstraint(
            "reserved_input_tokens > 0 AND reserved_output_tokens > 0",
            name="ck_thread_summary_dispatches_tokens_positive",
        ),
        CheckConstraint(
            "(reserved_cost_amount IS NULL AND reserved_currency IS NULL "
            "AND cost_is_estimate IS NULL) OR "
            "(reserved_cost_amount IS NOT NULL AND reserved_cost_amount >= 0 "
            "AND reserved_currency IS NOT NULL AND "
            "reserved_currency ~ '^[A-Z]{3}$' AND cost_is_estimate IS NOT NULL)",
            name="ck_thread_summary_dispatches_cost_pair",
        ),
        CheckConstraint(
            "octet_length(budget_reservation::text) <= 262144",
            name="ck_thread_summary_dispatches_reservation_size",
        ),
        Index(
            "ix_thread_summary_dispatches_outcome",
            "outcome",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reservation_id: Mapped[UUID] = mapped_column(
        SQLUuid(as_uuid=True), nullable=False, default=uuid4
    )
    pricing_snapshot_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    reserved_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    reserved_currency: Mapped[str | None] = mapped_column(String(3))
    cost_is_estimate: Mapped[bool | None] = mapped_column(Boolean)
    budget_reservation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    budget_reservation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=ThreadSummaryDispatchOutcome.INTENT_RECORDED.value,
        server_default=ThreadSummaryDispatchOutcome.INTENT_RECORDED.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ThreadSummaryResponse(Base):
    """Immutable provider output; a row never serves as a source-text copy."""

    __tablename__ = "thread_summary_responses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_responses_job_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id", "dispatch_id"],
            [
                "thread_summary_dispatches.workspace_id",
                "thread_summary_dispatches.thread_id",
                "thread_summary_dispatches.job_id",
                "thread_summary_dispatches.id",
            ],
            name="fk_thread_summary_responses_dispatch_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_responses_workspace_thread_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "id",
            name="uq_thread_summary_responses_workspace_thread_job_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_thread_summary_responses_job",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "dispatch_id",
            name="uq_thread_summary_responses_dispatch",
        ),
        CheckConstraint(
            "response_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_responses_hash_format",
        ),
        CheckConstraint(
            "octet_length(raw_response::text) <= 1048576",
            name="ck_thread_summary_responses_size",
        ),
        CheckConstraint(
            "octet_length(usage_metadata::text) <= 65536",
            name="ck_thread_summary_responses_usage_size",
        ),
        CheckConstraint(
            "(input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL "
            "AND cached_tokens IS NULL) OR "
            "(input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0 "
            "AND (cached_tokens IS NULL OR cached_tokens >= 0) "
            "AND total_tokens = input_tokens + output_tokens)",
            name="ck_thread_summary_responses_usage",
        ),
        CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL AND cost_is_estimate IS NULL) OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 "
            "AND cost_currency IS NOT NULL AND cost_currency ~ '^[A-Z]{3}$' "
            "AND cost_is_estimate IS NOT NULL)",
            name="ck_thread_summary_responses_cost",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dispatch_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    response_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    cost_is_estimate: Mapped[bool | None] = mapped_column(Boolean)
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadSummaryArtifact(Base):
    """An immutable validated summary revision with an append-only publication state."""

    __tablename__ = "thread_summary_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_artifacts_job_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id", "response_id"],
            [
                "thread_summary_responses.workspace_id",
                "thread_summary_responses.thread_id",
                "thread_summary_responses.job_id",
                "thread_summary_responses.id",
            ],
            name="fk_thread_summary_artifacts_response_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id", "parent_artifact_id"],
            [
                "thread_summary_artifacts.workspace_id",
                "thread_summary_artifacts.thread_id",
                "thread_summary_artifacts.id",
            ],
            name="fk_thread_summary_artifacts_parent_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_artifacts_workspace_thread_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_thread_summary_artifacts_job",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "response_id",
            name="uq_thread_summary_artifacts_job_response",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "id",
            name="uq_thread_summary_artifacts_workspace_thread_job_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "revision_number",
            name="uq_thread_summary_artifacts_revision_number",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "revision_hash",
            name="uq_thread_summary_artifacts_revision_hash",
        ),
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            "summary_identity_hash",
            name="uq_thread_summary_artifacts_identity",
        ),
        CheckConstraint(
            "revision_number > 0",
            name="ck_thread_summary_artifacts_revision_positive",
        ),
        CheckConstraint(
            "publication_status IN ('PENDING', 'PUBLISHED', 'STALE', 'FAILED')",
            name="ck_thread_summary_artifacts_publication_status",
        ),
        CheckConstraint(
            "(publication_status = 'PUBLISHED' AND published_at IS NOT NULL "
            "AND published_by IS NOT NULL) OR "
            "(publication_status <> 'PUBLISHED' AND published_at IS NULL)",
            name="ck_thread_summary_artifacts_published_at",
        ),
        CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$' "
            "AND content_hash ~ '^[0-9a-f]{64}$' "
            "AND source_index_hash ~ '^[0-9a-f]{64}$' "
            "AND summary_identity_hash ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_artifacts_hash_formats",
        ),
        CheckConstraint(
            "(parent_artifact_id IS NULL AND parent_revision_hash IS NULL) OR "
            "(parent_artifact_id IS NOT NULL AND parent_revision_hash IS NOT NULL AND "
            "parent_revision_hash ~ '^[0-9a-f]{64}$')",
            name="ck_thread_summary_artifacts_parent",
        ),
        CheckConstraint(
            "octet_length(summary_content::text) <= 1048576",
            name="ck_thread_summary_artifacts_content_size",
        ),
        CheckConstraint(
            "octet_length(source_index::text) <= 2097152",
            name="ck_thread_summary_artifacts_source_index_size",
        ),
        CheckConstraint(
            "octet_length(validation_result::text) <= 262144",
            name="ck_thread_summary_artifacts_validation_size",
        ),
        Index(
            "ix_thread_summary_artifacts_thread_status",
            "workspace_id",
            "thread_id",
            "publication_status",
            "revision_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    response_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    parent_artifact_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    parent_revision_hash: Mapped[str | None] = mapped_column(String(64))
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_index: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    source_index_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    publication_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ThreadSummaryArtifactStatus.PENDING.value,
        server_default=ThreadSummaryArtifactStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(128))


__all__ = [
    "ThreadSummaryArtifact",
    "ThreadSummaryArtifactStatus",
    "ThreadSummaryDispatch",
    "ThreadSummaryDispatchOutcome",
    "ThreadSummaryJob",
    "ThreadSummaryJobSource",
    "ThreadSummaryResponse",
]
