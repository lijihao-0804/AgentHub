"""Fix each summary job to one exact history-bound LangGraph checkpoint.

Revision ID: 0047_summary_checkpoint_binding
Revises: 0046_summary_review
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_summary_checkpoint_binding"
down_revision = "0046_summary_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "thread_summary_jobs",
        sa.Column("checkpoint_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "thread_summary_jobs",
        sa.Column("checkpoint_ns", sa.Text(), nullable=True),
    )
    op.add_column(
        "thread_summary_jobs",
        sa.Column("checkpoint_model_round_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "thread_summary_jobs",
        sa.Column("checkpoint_binding_sha256", sa.String(64), nullable=True),
    )

    # Old 0043-0046 rows cannot acquire an exact checkpoint identity
    # retroactively. They remain as retained history and keep failing closed.
    op.create_check_constraint(
        "ck_thread_summary_jobs_checkpoint_shape",
        "thread_summary_jobs",
        "(checkpoint_id IS NULL AND checkpoint_ns IS NULL "
        "AND checkpoint_model_round_count IS NULL "
        "AND checkpoint_binding_sha256 IS NULL) OR "
        "(checkpoint_id IS NOT NULL AND checkpoint_ns IS NOT NULL "
        "AND checkpoint_model_round_count IS NOT NULL "
        "AND checkpoint_binding_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_thread_summary_jobs_checkpoint_id_nonempty",
        "thread_summary_jobs",
        "checkpoint_id IS NULL OR length(checkpoint_id) > 0",
    )
    op.create_check_constraint(
        "ck_thread_summary_jobs_checkpoint_selector_size",
        "thread_summary_jobs",
        "(checkpoint_id IS NULL OR octet_length(checkpoint_id) <= 1024) "
        "AND (checkpoint_ns IS NULL OR octet_length(checkpoint_ns) <= 1024)",
    )
    op.create_check_constraint(
        "ck_thread_summary_jobs_checkpoint_round_bounds",
        "thread_summary_jobs",
        "checkpoint_model_round_count IS NULL OR "
        "(checkpoint_model_round_count >= 0 "
        "AND checkpoint_model_round_count <= 2147483647)",
    )
    op.create_check_constraint(
        "ck_thread_summary_jobs_checkpoint_round_relation",
        "thread_summary_jobs",
        "model_round IS NULL OR checkpoint_id IS NULL OR "
        "checkpoint_model_round_count = model_round - 1",
    )
    op.create_check_constraint(
        "ck_thread_summary_jobs_checkpoint_hash",
        "thread_summary_jobs",
        "checkpoint_binding_sha256 IS NULL OR "
        "checkpoint_binding_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_unique_constraint(
        "uq_thread_summary_jobs_exact_checkpoint",
        "thread_summary_jobs",
        ["workspace_id", "trigger_run_id", "checkpoint_id", "checkpoint_ns"],
    )

    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_checkpoint_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.model_round IS NULL OR (
              NEW.checkpoint_id IS NULL
              OR NEW.checkpoint_ns IS NULL
              OR NEW.checkpoint_model_round_count IS NULL
              OR NEW.checkpoint_binding_sha256 IS NULL
            ) THEN
              RAISE EXCEPTION
                'New summary jobs require one exact checkpoint binding';
            END IF;
            RETURN NEW;
          END IF;

          IF (
            NEW.checkpoint_id,
            NEW.checkpoint_ns,
            NEW.checkpoint_model_round_count,
            NEW.checkpoint_binding_sha256
          ) IS DISTINCT FROM (
            OLD.checkpoint_id,
            OLD.checkpoint_ns,
            OLD.checkpoint_model_round_count,
            OLD.checkpoint_binding_sha256
          ) THEN
            RAISE EXCEPTION 'Thread summary checkpoint identity is immutable';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER thread_summary_checkpoint_binding_guard
          BEFORE INSERT OR UPDATE ON thread_summary_jobs
          FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_checkpoint_binding();
        """
    )


def downgrade() -> None:
    # Removing these columns would erase the only durable proof of which exact
    # framework checkpoint authorized a summary dispatch.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM thread_summary_jobs
            WHERE checkpoint_id IS NOT NULL
               OR checkpoint_ns IS NOT NULL
               OR checkpoint_model_round_count IS NOT NULL
               OR checkpoint_binding_sha256 IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'Cannot remove retained summary checkpoint identities';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER thread_summary_checkpoint_binding_guard "
        "ON thread_summary_jobs"
    )
    op.execute("DROP FUNCTION thread_summary_guard_checkpoint_binding()")
    op.drop_constraint(
        "uq_thread_summary_jobs_exact_checkpoint",
        "thread_summary_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_checkpoint_hash",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_checkpoint_round_relation",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_checkpoint_round_bounds",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_checkpoint_selector_size",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_checkpoint_id_nonempty",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_thread_summary_jobs_checkpoint_shape",
        "thread_summary_jobs",
        type_="check",
    )
    op.drop_column("thread_summary_jobs", "checkpoint_binding_sha256")
    op.drop_column("thread_summary_jobs", "checkpoint_model_round_count")
    op.drop_column("thread_summary_jobs", "checkpoint_ns")
    op.drop_column("thread_summary_jobs", "checkpoint_id")
