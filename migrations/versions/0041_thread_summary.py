"""Persist thread summary jobs, dispatch evidence and immutable revisions."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_thread_summary"
down_revision: str | None = "0040_thread_history_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    now = sa.func.now()

    op.create_table(
        "thread_summary_jobs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("trigger_run_id", uuid_type, nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("source_watermark", sa.Integer(), nullable=False),
        sa.Column("source_scope", jsonb, nullable=False),
        sa.Column("source_scope_hash", sa.String(64), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("parent_artifact_id", uuid_type),
        sa.Column("parent_revision_hash", sa.String(64)),
        sa.Column("policy_spec", jsonb, nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("budget_version", sa.String(64), nullable=False),
        sa.Column("model_spec", jsonb, nullable=False),
        sa.Column("model_spec_hash", sa.String(64), nullable=False),
        sa.Column("tokenizer_id", sa.String(128), nullable=False),
        sa.Column("tokenizer_version", sa.String(64), nullable=False),
        sa.Column("target_budget_tokens", sa.Integer(), nullable=False),
        sa.Column("permission_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "budget_reserved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dispatch_id", uuid_type),
        sa.Column("response_id", uuid_type),
        sa.Column("artifact_id", uuid_type),
        sa.Column("failure_code", sa.String(96)),
        sa.Column("validation_result", jsonb),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_intent_at", sa.DateTime(timezone=True)),
        sa.Column("response_stored_at", sa.DateTime(timezone=True)),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_thread_summary_jobs_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_thread_summary_jobs_thread_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trigger_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_thread_summary_jobs_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_thread_summary_jobs_created_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_jobs_workspace_thread_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "identity_hash",
            name="uq_thread_summary_jobs_identity",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'CLAIMED', 'DISPATCH_INTENT', 'RESPONSE_STORED', "
            "'VALIDATED', 'PUBLISHED', 'FAILED', 'STALE', 'UNKNOWN_OUTCOME')",
            name="ck_thread_summary_jobs_status",
        ),
        sa.CheckConstraint(
            "source_watermark > 0",
            name="ck_thread_summary_jobs_watermark_positive",
        ),
        sa.CheckConstraint(
            "target_budget_tokens > 0",
            name="ck_thread_summary_jobs_target_budget_positive",
        ),
        sa.CheckConstraint(
            "octet_length(source_scope::text) <= 1048576",
            name="ck_thread_summary_jobs_source_scope_size",
        ),
        sa.CheckConstraint(
            "octet_length(policy_spec::text) <= 262144",
            name="ck_thread_summary_jobs_policy_spec_size",
        ),
        sa.CheckConstraint(
            "octet_length(model_spec::text) <= 262144",
            name="ck_thread_summary_jobs_model_spec_size",
        ),
        sa.CheckConstraint(
            "identity_hash ~ '^[0-9a-f]{64}$' "
            "AND source_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND source_content_hash ~ '^[0-9a-f]{64}$' "
            "AND model_spec_hash ~ '^[0-9a-f]{64}$' "
            "AND permission_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_jobs_hash_formats",
        ),
        sa.CheckConstraint(
            "(parent_artifact_id IS NULL AND parent_revision_hash IS NULL) OR "
            "(parent_artifact_id IS NOT NULL AND parent_revision_hash IS NOT NULL AND "
            "parent_revision_hash ~ '^[0-9a-f]{64}$')",
            name="ck_thread_summary_jobs_parent",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_thread_summary_jobs_lease_pair",
        ),
        sa.CheckConstraint(
            "lease_token IS NULL OR length(lease_token) > 0",
            name="ck_thread_summary_jobs_lease_nonempty",
        ),
        sa.CheckConstraint(
            "status <> 'QUEUED' OR lease_token IS NULL",
            name="ck_thread_summary_jobs_queued_without_lease",
        ),
        sa.CheckConstraint(
            "status NOT IN ('CLAIMED', 'DISPATCH_INTENT') OR lease_token IS NOT NULL",
            name="ck_thread_summary_jobs_active_lease",
        ),
        sa.CheckConstraint(
            "status <> 'UNKNOWN_OUTCOME' OR lease_token IS NULL",
            name="ck_thread_summary_jobs_unknown_without_lease",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('QUEUED', 'CLAIMED')) OR budget_reserved = false",
            name="ck_thread_summary_jobs_pre_dispatch_budget",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('DISPATCH_INTENT', 'RESPONSE_STORED', 'VALIDATED', "
            "'PUBLISHED', 'STALE', 'UNKNOWN_OUTCOME')) OR budget_reserved = true",
            name="ck_thread_summary_jobs_reserved_budget",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('DISPATCH_INTENT', 'RESPONSE_STORED', 'VALIDATED', "
            "'PUBLISHED', 'STALE', 'UNKNOWN_OUTCOME')) OR dispatch_id IS NOT NULL",
            name="ck_thread_summary_jobs_dispatch_pointer",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('RESPONSE_STORED', 'VALIDATED', 'PUBLISHED', 'STALE')) "
            "OR response_id IS NOT NULL",
            name="ck_thread_summary_jobs_response_pointer",
        ),
        sa.CheckConstraint(
            "status <> 'PUBLISHED' OR artifact_id IS NOT NULL",
            name="ck_thread_summary_jobs_published_pointer",
        ),
    )
    op.create_index(
        "ix_thread_summary_jobs_thread_status",
        "thread_summary_jobs",
        ["workspace_id", "thread_id", "status", "created_at"],
    )
    op.create_index(
        "ix_thread_summary_jobs_lease",
        "thread_summary_jobs",
        ["status", "lease_expires_at", "id"],
    )

    op.create_table(
        "thread_summary_job_sources",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("job_id", uuid_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("turn_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("original_ref", sa.String(128), nullable=False),
        sa.Column("source_identity_hash", sa.String(64), nullable=False),
        sa.Column("revision_sha256", sa.String(64), nullable=False),
        sa.Column("user_input_sha256", sa.String(64), nullable=False),
        sa.Column("final_output_sha256", sa.String(64), nullable=False),
        sa.Column(
            "artifact_identities",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("summary_eligibility", sa.String(16), nullable=False),
        sa.Column("eligibility_reason", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_job_sources_job_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_thread_summary_job_sources_turn_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_thread_summary_job_sources_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "job_id",
            "ordinal",
            name="uq_thread_summary_job_sources_ordinal",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "job_id",
            "source_id",
            name="uq_thread_summary_job_sources_source_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "job_id",
            "sequence",
            name="uq_thread_summary_job_sources_sequence",
        ),
        sa.CheckConstraint(
            "ordinal > 0 AND sequence > 0",
            name="ck_thread_summary_job_sources_positive_order",
        ),
        sa.CheckConstraint(
            "source_identity_hash ~ '^[0-9a-f]{64}$' "
            "AND revision_sha256 ~ '^[0-9a-f]{64}$' "
            "AND user_input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND final_output_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_job_sources_hash_formats",
        ),
        sa.CheckConstraint(
            "summary_eligibility IN ('ELIGIBLE', 'INELIGIBLE', 'UNKNOWN')",
            name="ck_thread_summary_job_sources_eligibility",
        ),
        sa.CheckConstraint(
            "octet_length(artifact_identities::text) <= 1048576",
            name="ck_thread_summary_job_sources_artifacts_size",
        ),
    )
    op.create_index(
        "ix_thread_summary_job_sources_run",
        "thread_summary_job_sources",
        ["workspace_id", "thread_id", "run_id"],
    )

    op.create_table(
        "thread_summary_dispatches",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("job_id", uuid_type, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("reservation_id", uuid_type, nullable=False),
        sa.Column("pricing_snapshot_id", uuid_type),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_amount", sa.Numeric(20, 8)),
        sa.Column("reserved_currency", sa.String(3)),
        sa.Column("cost_is_estimate", sa.Boolean()),
        sa.Column("budget_reservation", jsonb, nullable=False),
        sa.Column("budget_reservation_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False, server_default="INTENT_RECORDED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_dispatches_job_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pricing_snapshot_id"],
            ["pricing_snapshots.workspace_id", "pricing_snapshots.id"],
            name="fk_thread_summary_dispatches_pricing_workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_dispatches_workspace_thread_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "id",
            name="uq_thread_summary_dispatches_workspace_thread_job_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_thread_summary_dispatches_job",
        ),
        sa.UniqueConstraint("reservation_id", name="uq_thread_summary_dispatches_reservation"),
        sa.CheckConstraint(
            "attempt_number = 1",
            name="ck_thread_summary_dispatches_single_attempt",
        ),
        sa.CheckConstraint(
            "outcome IN ('INTENT_RECORDED', 'DISPATCHED', 'RESPONSE_RECEIVED', "
            "'FAILED_NOT_DISPATCHED', 'UNKNOWN_OUTCOME')",
            name="ck_thread_summary_dispatches_outcome",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' "
            "AND intent_hash ~ '^[0-9a-f]{64}$' "
            "AND budget_reservation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_dispatches_hash_formats",
        ),
        sa.CheckConstraint(
            "reserved_input_tokens > 0 AND reserved_output_tokens > 0",
            name="ck_thread_summary_dispatches_tokens_positive",
        ),
        sa.CheckConstraint(
            "(reserved_cost_amount IS NULL AND reserved_currency IS NULL "
            "AND cost_is_estimate IS NULL) OR "
            "(reserved_cost_amount IS NOT NULL AND reserved_cost_amount >= 0 "
            "AND reserved_currency IS NOT NULL AND "
            "reserved_currency ~ '^[A-Z]{3}$' AND cost_is_estimate IS NOT NULL)",
            name="ck_thread_summary_dispatches_cost_pair",
        ),
        sa.CheckConstraint(
            "octet_length(budget_reservation::text) <= 262144",
            name="ck_thread_summary_dispatches_reservation_size",
        ),
    )
    op.create_index(
        "ix_thread_summary_dispatches_outcome",
        "thread_summary_dispatches",
        ["outcome", "updated_at", "id"],
    )

    op.create_table(
        "thread_summary_responses",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("job_id", uuid_type, nullable=False),
        sa.Column("dispatch_id", uuid_type, nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("response_sha256", sa.String(64), nullable=False),
        sa.Column("raw_response", jsonb, nullable=False),
        sa.Column(
            "usage_metadata",
            jsonb,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("cost_amount", sa.Numeric(20, 8)),
        sa.Column("cost_currency", sa.String(3)),
        sa.Column("cost_is_estimate", sa.Boolean()),
        sa.Column("finish_reason", sa.String(64)),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_responses_job_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_responses_workspace_thread_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "id",
            name="uq_thread_summary_responses_workspace_thread_job_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_thread_summary_responses_job",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "dispatch_id",
            name="uq_thread_summary_responses_dispatch",
        ),
        sa.CheckConstraint(
            "response_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_responses_hash_format",
        ),
        sa.CheckConstraint(
            "octet_length(raw_response::text) <= 1048576",
            name="ck_thread_summary_responses_size",
        ),
        sa.CheckConstraint(
            "octet_length(usage_metadata::text) <= 65536",
            name="ck_thread_summary_responses_usage_size",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL "
            "AND cached_tokens IS NULL) OR "
            "(input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0 "
            "AND (cached_tokens IS NULL OR cached_tokens >= 0) "
            "AND total_tokens = input_tokens + output_tokens)",
            name="ck_thread_summary_responses_usage",
        ),
        sa.CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL AND cost_is_estimate IS NULL) OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 "
            "AND cost_currency IS NOT NULL AND cost_currency ~ '^[A-Z]{3}$' "
            "AND cost_is_estimate IS NOT NULL)",
            name="ck_thread_summary_responses_cost",
        ),
    )

    op.create_table(
        "thread_summary_artifacts",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("job_id", uuid_type, nullable=False),
        sa.Column("response_id", uuid_type, nullable=False),
        sa.Column("parent_artifact_id", uuid_type),
        sa.Column("parent_revision_hash", sa.String(64)),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("revision_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("summary_identity_hash", sa.String(64), nullable=False),
        sa.Column("summary_content", jsonb, nullable=False),
        sa.Column("source_index", jsonb, nullable=False),
        sa.Column("source_index_hash", sa.String(64), nullable=False),
        sa.Column("validator_version", sa.String(64), nullable=False),
        sa.Column("validation_result", jsonb, nullable=False),
        sa.Column(
            "publication_status",
            sa.String(16),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("published_by", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_thread_summary_artifacts_job_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "parent_artifact_id"],
            [
                "thread_summary_artifacts.workspace_id",
                "thread_summary_artifacts.thread_id",
                "thread_summary_artifacts.id",
            ],
            name="fk_thread_summary_artifacts_parent_workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_thread_summary_artifacts_workspace_thread_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_thread_summary_artifacts_job",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "response_id",
            name="uq_thread_summary_artifacts_job_response",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            "id",
            name="uq_thread_summary_artifacts_workspace_thread_job_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "revision_number",
            name="uq_thread_summary_artifacts_revision_number",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "revision_hash",
            name="uq_thread_summary_artifacts_revision_hash",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "summary_identity_hash",
            name="uq_thread_summary_artifacts_identity",
        ),
        sa.CheckConstraint(
            "revision_number > 0",
            name="ck_thread_summary_artifacts_revision_positive",
        ),
        sa.CheckConstraint(
            "publication_status IN ('PENDING', 'PUBLISHED', 'STALE', 'FAILED')",
            name="ck_thread_summary_artifacts_publication_status",
        ),
        sa.CheckConstraint(
            "(publication_status = 'PUBLISHED' AND published_at IS NOT NULL "
            "AND published_by IS NOT NULL) OR "
            "(publication_status <> 'PUBLISHED' AND published_at IS NULL)",
            name="ck_thread_summary_artifacts_published_at",
        ),
        sa.CheckConstraint(
            "revision_hash ~ '^[0-9a-f]{64}$' "
            "AND content_hash ~ '^[0-9a-f]{64}$' "
            "AND source_index_hash ~ '^[0-9a-f]{64}$' "
            "AND summary_identity_hash ~ '^[0-9a-f]{64}$'",
            name="ck_thread_summary_artifacts_hash_formats",
        ),
        sa.CheckConstraint(
            "(parent_artifact_id IS NULL AND parent_revision_hash IS NULL) OR "
            "(parent_artifact_id IS NOT NULL AND parent_revision_hash IS NOT NULL AND "
            "parent_revision_hash ~ '^[0-9a-f]{64}$')",
            name="ck_thread_summary_artifacts_parent",
        ),
        sa.CheckConstraint(
            "octet_length(summary_content::text) <= 1048576",
            name="ck_thread_summary_artifacts_content_size",
        ),
        sa.CheckConstraint(
            "octet_length(source_index::text) <= 2097152",
            name="ck_thread_summary_artifacts_source_index_size",
        ),
        sa.CheckConstraint(
            "octet_length(validation_result::text) <= 262144",
            name="ck_thread_summary_artifacts_validation_size",
        ),
    )
    op.create_index(
        "ix_thread_summary_artifacts_thread_status",
        "thread_summary_artifacts",
        ["workspace_id", "thread_id", "publication_status", "revision_number"],
    )

    # These pointers form cycles with their evidence rows. They are installed
    # after all parents exist; nullable columns keep job creation one-step.
    op.create_foreign_key(
        "fk_thread_summary_jobs_parent_artifact_workspace",
        "thread_summary_jobs",
        "thread_summary_artifacts",
        ["workspace_id", "thread_id", "parent_artifact_id"],
        ["workspace_id", "thread_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_thread_summary_jobs_dispatch_scope",
        "thread_summary_jobs",
        "thread_summary_dispatches",
        ["workspace_id", "thread_id", "id", "dispatch_id"],
        ["workspace_id", "thread_id", "job_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_thread_summary_jobs_response_scope",
        "thread_summary_jobs",
        "thread_summary_responses",
        ["workspace_id", "thread_id", "id", "response_id"],
        ["workspace_id", "thread_id", "job_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_thread_summary_jobs_artifact_scope",
        "thread_summary_jobs",
        "thread_summary_artifacts",
        ["workspace_id", "thread_id", "id", "artifact_id"],
        ["workspace_id", "thread_id", "job_id", "id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_job_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Thread summary jobs are retained';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
              SELECT 1 FROM agent_runs
              WHERE workspace_id = NEW.workspace_id
                AND id = NEW.trigger_run_id
                AND thread_id = NEW.thread_id
            ) THEN
              RAISE EXCEPTION 'Summary trigger Run does not belong to the Thread';
            END IF;
            IF NEW.status IN ('CLAIMED', 'DISPATCH_INTENT')
              OR NEW.budget_reserved
              OR NEW.dispatch_id IS NOT NULL
              OR NEW.response_id IS NOT NULL
              OR NEW.artifact_id IS NOT NULL
            THEN
              RAISE EXCEPTION 'Thread summary job must start before dispatch';
            END IF;
            RETURN NEW;
          END IF;
          IF (
            NEW.workspace_id, NEW.thread_id, NEW.trigger_run_id, NEW.identity_hash,
            NEW.source_watermark, NEW.source_scope, NEW.source_scope_hash,
            NEW.source_content_hash, NEW.policy_spec, NEW.policy_version,
            NEW.prompt_version, NEW.budget_version, NEW.model_spec, NEW.model_spec_hash,
            NEW.tokenizer_id, NEW.tokenizer_version, NEW.target_budget_tokens,
            NEW.permission_snapshot_hash, NEW.created_by
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.thread_id, OLD.trigger_run_id, OLD.identity_hash,
            OLD.source_watermark, OLD.source_scope, OLD.source_scope_hash,
            OLD.source_content_hash, OLD.policy_spec, OLD.policy_version,
            OLD.prompt_version, OLD.budget_version, OLD.model_spec, OLD.model_spec_hash,
            OLD.tokenizer_id, OLD.tokenizer_version, OLD.target_budget_tokens,
            OLD.permission_snapshot_hash, OLD.created_by
          ) THEN
            RAISE EXCEPTION 'Thread summary job identity is immutable';
          END IF;
          IF NEW.dispatch_id IS DISTINCT FROM OLD.dispatch_id
            AND NEW.dispatch_id IS NOT NULL AND OLD.dispatch_id IS NOT NULL
          THEN
            RAISE EXCEPTION 'Thread summary dispatch pointer is append-only';
          END IF;
          IF NEW.response_id IS DISTINCT FROM OLD.response_id
            AND NEW.response_id IS NOT NULL AND OLD.response_id IS NOT NULL
          THEN
            RAISE EXCEPTION 'Thread summary response pointer is append-only';
          END IF;
          IF NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
            AND NEW.artifact_id IS NOT NULL AND OLD.artifact_id IS NOT NULL
          THEN
            RAISE EXCEPTION 'Thread summary artifact pointer is append-only';
          END IF;
          IF OLD.status = 'UNKNOWN_OUTCOME' AND NEW.status <> 'UNKNOWN_OUTCOME' THEN
            RAISE EXCEPTION 'Unknown Thread summary outcome is terminal';
          END IF;
          IF NOT (
            (OLD.status = 'QUEUED' AND NEW.status IN ('QUEUED', 'CLAIMED', 'FAILED')) OR
            (OLD.status = 'CLAIMED' AND NEW.status IN (
              'QUEUED', 'CLAIMED', 'DISPATCH_INTENT', 'FAILED'
            )) OR
            (OLD.status = 'DISPATCH_INTENT' AND NEW.status IN (
              'DISPATCH_INTENT', 'RESPONSE_STORED', 'UNKNOWN_OUTCOME'
            )) OR
            (OLD.status = 'RESPONSE_STORED' AND NEW.status IN (
              'RESPONSE_STORED', 'VALIDATED', 'FAILED', 'STALE'
            )) OR
            (OLD.status = 'VALIDATED' AND NEW.status IN (
              'VALIDATED', 'PUBLISHED', 'FAILED', 'STALE'
            )) OR
            OLD.status = NEW.status
          ) THEN
            RAISE EXCEPTION 'Thread summary job transition is invalid';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER thread_summary_job_scope_guard
          BEFORE INSERT OR UPDATE OR DELETE ON thread_summary_jobs
          FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_job_scope();
        """
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_source() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'Thread summary source coordinates are immutable';
          END IF;
          IF NOT EXISTS (
            SELECT 1
            FROM thread_turns AS turn
            JOIN agent_runs AS run
              ON run.workspace_id = turn.workspace_id
             AND run.id = NEW.run_id
             AND run.thread_turn_id = turn.id
             AND run.thread_id = NEW.thread_id
            WHERE turn.workspace_id = NEW.workspace_id
              AND turn.id = NEW.turn_id
              AND turn.thread_id = NEW.thread_id
              AND turn.agent_run_id = NEW.run_id
          ) THEN
            RAISE EXCEPTION 'Summary source does not bind one complete Thread exchange';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER thread_summary_source_guard
          BEFORE INSERT OR UPDATE OR DELETE ON thread_summary_job_sources
          FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_source();
        """
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_dispatch() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Thread summary dispatch ledger is retained';
          END IF;
          IF (
            NEW.workspace_id, NEW.thread_id, NEW.job_id, NEW.attempt_number,
            NEW.request_hash, NEW.intent_hash, NEW.reservation_id,
            NEW.pricing_snapshot_id, NEW.reserved_input_tokens,
            NEW.reserved_output_tokens, NEW.reserved_cost_amount,
            NEW.reserved_currency, NEW.cost_is_estimate,
            NEW.budget_reservation, NEW.budget_reservation_hash, NEW.created_at
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.thread_id, OLD.job_id, OLD.attempt_number,
            OLD.request_hash, OLD.intent_hash, OLD.reservation_id,
            OLD.pricing_snapshot_id, OLD.reserved_input_tokens,
            OLD.reserved_output_tokens, OLD.reserved_cost_amount,
            OLD.reserved_currency, OLD.cost_is_estimate,
            OLD.budget_reservation, OLD.budget_reservation_hash, OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Thread summary dispatch intent is immutable';
          END IF;
          IF OLD.outcome <> NEW.outcome AND NOT (
            (OLD.outcome = 'INTENT_RECORDED' AND NEW.outcome IN (
              'DISPATCHED', 'FAILED_NOT_DISPATCHED', 'UNKNOWN_OUTCOME'
            )) OR
            (OLD.outcome = 'DISPATCHED' AND NEW.outcome IN (
              'RESPONSE_RECEIVED', 'UNKNOWN_OUTCOME'
            ))
          ) THEN
            RAISE EXCEPTION 'Thread summary dispatch outcome transition is invalid';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER thread_summary_dispatch_guard
          BEFORE UPDATE OR DELETE ON thread_summary_dispatches
          FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_dispatch();
        """
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_require_dispatch_commit() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM thread_summary_jobs AS job
            JOIN thread_summary_dispatches AS dispatch
              ON dispatch.workspace_id = job.workspace_id
             AND dispatch.thread_id = job.thread_id
             AND dispatch.job_id = job.id
             AND dispatch.id = job.dispatch_id
            WHERE job.workspace_id = NEW.workspace_id
              AND job.id = NEW.job_id
              AND job.status IN (
                'DISPATCH_INTENT', 'RESPONSE_STORED', 'VALIDATED', 'PUBLISHED',
                'STALE', 'UNKNOWN_OUTCOME'
              )
              AND job.budget_reserved
          ) THEN
            RAISE EXCEPTION 'Dispatch intent and budget reservation must commit together';
          END IF;
          RETURN NULL;
        END
        $$;
        CREATE CONSTRAINT TRIGGER thread_summary_dispatch_commit_guard
          AFTER INSERT ON thread_summary_dispatches
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION thread_summary_require_dispatch_commit();
        """
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_response() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'Thread summary responses are immutable';
        END
        $$;
        CREATE TRIGGER thread_summary_response_guard
          BEFORE UPDATE OR DELETE ON thread_summary_responses
          FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_response();
        """
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_artifact() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Thread summary artifacts are retained';
          END IF;
          IF (
            NEW.workspace_id, NEW.thread_id, NEW.job_id, NEW.response_id,
            NEW.parent_artifact_id, NEW.parent_revision_hash, NEW.revision_number,
            NEW.revision_hash, NEW.content_hash, NEW.summary_identity_hash,
            NEW.summary_content, NEW.source_index, NEW.source_index_hash,
            NEW.validator_version, NEW.validation_result, NEW.created_at
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.thread_id, OLD.job_id, OLD.response_id,
            OLD.parent_artifact_id, OLD.parent_revision_hash, OLD.revision_number,
            OLD.revision_hash, OLD.content_hash, OLD.summary_identity_hash,
            OLD.summary_content, OLD.source_index, OLD.source_index_hash,
            OLD.validator_version, OLD.validation_result, OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Thread summary artifact content is immutable';
          END IF;
          IF OLD.publication_status = 'PENDING' THEN
            IF NEW.publication_status NOT IN ('PUBLISHED', 'STALE', 'FAILED') THEN
              RAISE EXCEPTION 'Summary artifact publication transition is invalid';
            END IF;
          ELSIF OLD.publication_status IN ('PUBLISHED', 'STALE', 'FAILED') THEN
            IF NEW.publication_status IS DISTINCT FROM OLD.publication_status
              OR NEW.published_at IS DISTINCT FROM OLD.published_at
              OR NEW.published_by IS DISTINCT FROM OLD.published_by
            THEN
              RAISE EXCEPTION 'Published or terminal summary artifacts are immutable';
            END IF;
          ELSE
            RAISE EXCEPTION 'Unknown summary artifact status';
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER thread_summary_artifact_guard
          BEFORE UPDATE OR DELETE ON thread_summary_artifacts
          FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_artifact();
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_thread_summary_jobs_artifact_scope",
        "thread_summary_jobs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_thread_summary_jobs_response_scope",
        "thread_summary_jobs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_thread_summary_jobs_dispatch_scope",
        "thread_summary_jobs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_thread_summary_jobs_parent_artifact_workspace",
        "thread_summary_jobs",
        type_="foreignkey",
    )
    op.execute("DROP TRIGGER thread_summary_artifact_guard ON thread_summary_artifacts")
    op.execute("DROP FUNCTION thread_summary_guard_artifact()")
    op.execute("DROP TRIGGER thread_summary_response_guard ON thread_summary_responses")
    op.execute("DROP FUNCTION thread_summary_guard_response()")
    op.execute(
        "DROP TRIGGER thread_summary_dispatch_commit_guard "
        "ON thread_summary_dispatches"
    )
    op.execute("DROP FUNCTION thread_summary_require_dispatch_commit()")
    op.execute("DROP TRIGGER thread_summary_dispatch_guard ON thread_summary_dispatches")
    op.execute("DROP FUNCTION thread_summary_guard_dispatch()")
    op.execute(
        "DROP TRIGGER thread_summary_source_guard ON thread_summary_job_sources"
    )
    op.execute("DROP FUNCTION thread_summary_guard_source()")
    op.execute("DROP TRIGGER thread_summary_job_scope_guard ON thread_summary_jobs")
    op.execute("DROP FUNCTION thread_summary_guard_job_scope()")
    op.drop_index(
        "ix_thread_summary_artifacts_thread_status",
        table_name="thread_summary_artifacts",
    )
    op.drop_table("thread_summary_artifacts")
    op.drop_table("thread_summary_responses")
    op.drop_index(
        "ix_thread_summary_dispatches_outcome",
        table_name="thread_summary_dispatches",
    )
    op.drop_table("thread_summary_dispatches")
    op.drop_index(
        "ix_thread_summary_job_sources_run",
        table_name="thread_summary_job_sources",
    )
    op.drop_table("thread_summary_job_sources")
    op.drop_index("ix_thread_summary_jobs_lease", table_name="thread_summary_jobs")
    op.drop_index(
        "ix_thread_summary_jobs_thread_status",
        table_name="thread_summary_jobs",
    )
    op.drop_table("thread_summary_jobs")
