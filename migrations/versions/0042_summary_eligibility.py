"""Add immutable source eligibility review records.

Revision ID: 0042_summary_eligibility
Revises: 0041_thread_summary
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_summary_eligibility"
down_revision = "0041_thread_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_summary_eligibility_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("reviewed_from_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_identity", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_summary_eligibility_thread",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_summary_eligibility_turn",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_eligibility_source_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "reviewed_from_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_eligibility_review_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_summary_eligibility_reviewer",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "source_identity",
            "revision",
            name="uq_summary_eligibility_revision",
        ),
        sa.CheckConstraint(
            "decision IN ('ELIGIBLE', 'INELIGIBLE')", name="ck_summary_eligibility_decision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_summary_eligibility_revision"),
        sa.CheckConstraint(
            "source_identity ~ '^[0-9a-f]{64}$'", name="ck_summary_eligibility_source_identity"
        ),
        sa.CheckConstraint(
            "(decision = 'ELIGIBLE' AND reason_code = 'ADMIN_REVIEWED') OR "
            "(decision = 'INELIGIBLE' AND reason_code = 'KEEP_EXACT')",
            name="ck_summary_eligibility_reason",
        ),
    )
    op.create_index(
        "ix_summary_eligibility_latest",
        "thread_summary_eligibility_records",
        ["workspace_id", "thread_id", "source_identity", "revision"],
    )
    op.execute("""
        CREATE FUNCTION thread_summary_eligibility_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'Summary eligibility reviews are immutable';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM thread_turns t
            JOIN agent_runs r ON r.workspace_id = t.workspace_id AND r.id = t.agent_run_id
            JOIN agent_runs trigger_run ON trigger_run.workspace_id = NEW.workspace_id
              AND trigger_run.id = NEW.reviewed_from_run_id
            WHERE t.workspace_id = NEW.workspace_id AND t.thread_id = NEW.thread_id
              AND t.id = NEW.turn_id AND r.id = NEW.run_id
              AND r.thread_id = NEW.thread_id AND r.thread_turn_id = t.id
              AND r.status = 'SUCCEEDED' AND trigger_run.thread_id = NEW.thread_id
          ) THEN
            RAISE EXCEPTION 'Summary eligibility source scope is invalid';
          END IF;
          RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER thread_summary_eligibility_guard_trigger
        BEFORE INSERT OR UPDATE OR DELETE ON thread_summary_eligibility_records
        FOR EACH ROW EXECUTE FUNCTION thread_summary_eligibility_guard()
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER thread_summary_eligibility_guard_trigger "
        "ON thread_summary_eligibility_records"
    )
    op.execute("DROP FUNCTION thread_summary_eligibility_guard()")
    op.drop_index("ix_summary_eligibility_latest", table_name="thread_summary_eligibility_records")
    op.drop_table("thread_summary_eligibility_records")
