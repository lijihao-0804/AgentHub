"""Bind each summary job to one trigger-Run model-round boundary.

Revision ID: 0043_summary_dispatch_boundary
Revises: 0042_summary_eligibility
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_summary_dispatch_boundary"
down_revision = "0042_summary_eligibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Null is retained only for pre-0043 history. New jobs require this
    # value in the service, and dispatch fences reject null-boundary jobs.
    op.add_column(
        "thread_summary_jobs",
        sa.Column("model_round", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_thread_summary_jobs_model_round_bounds",
        "thread_summary_jobs",
        "model_round IS NULL OR "
        "(model_round > 0 AND model_round <= 2147483647)",
    )
    op.create_unique_constraint(
        "uq_thread_summary_jobs_dispatch_boundary",
        "thread_summary_jobs",
        ["workspace_id", "trigger_run_id", "model_round"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION thread_summary_guard_job_scope() RETURNS trigger
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
            NEW.workspace_id, NEW.thread_id, NEW.trigger_run_id, NEW.model_round,
            NEW.identity_hash, NEW.source_watermark, NEW.source_scope,
            NEW.source_scope_hash, NEW.source_content_hash, NEW.policy_spec,
            NEW.policy_version, NEW.prompt_version, NEW.budget_version,
            NEW.model_spec, NEW.model_spec_hash, NEW.tokenizer_id,
            NEW.tokenizer_version, NEW.target_budget_tokens,
            NEW.permission_snapshot_hash, NEW.created_by
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.thread_id, OLD.trigger_run_id, OLD.model_round,
            OLD.identity_hash, OLD.source_watermark, OLD.source_scope,
            OLD.source_scope_hash, OLD.source_content_hash, OLD.policy_spec,
            OLD.policy_version, OLD.prompt_version, OLD.budget_version,
            OLD.model_spec, OLD.model_spec_hash, OLD.tokenizer_id,
            OLD.tokenizer_version, OLD.target_budget_tokens,
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
        """
    )


def downgrade() -> None:
    # A rollback must not silently erase the only persisted boundary identity
    # of already-created jobs. Empty/legacy-only installations may roll back.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM thread_summary_jobs WHERE model_round IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot drop persisted summary dispatch boundaries';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION thread_summary_guard_job_scope() RETURNS trigger
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
        """
    )
    op.drop_constraint(
        "uq_thread_summary_jobs_dispatch_boundary",
        "thread_summary_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_model_round_bounds",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_column("thread_summary_jobs", "model_round")
