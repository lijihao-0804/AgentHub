"""Opt-in personal delegation, decision provenance and exact-action denial history."""

import sqlalchemy as sa
from alembic import op

revision = "0057_delegated_approvals"
down_revision = "0056_auxiliary_run_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_preferences",
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("max_auto_denials", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "level IN ('MANUAL','LOW','MEDIUM')", name="ck_approval_preference_level"
        ),
        sa.CheckConstraint(
            "max_auto_denials BETWEEN 0 AND 10", name="ck_approval_preference_limit"
        ),
    )
    op.create_table(
        "personal_approval_runs",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            ondelete="CASCADE",
        ),
    )
    op.add_column("approvals", sa.Column("decision_source", sa.String(16)))
    op.add_column("approvals", sa.Column("routing_reason", sa.String(32)))
    op.execute(
        "UPDATE approvals SET decision_source='HUMAN' WHERE decided_by IS NOT NULL AND decision_status IN ('APPROVED','DENIED')"
    )
    op.create_check_constraint(
        "ck_approvals_decision_source",
        "approvals",
        "decision_source IN ('HUMAN','DELEGATED') AND decision_status IN ('APPROVED','DENIED') OR decision_source IS NULL",
    )
    op.create_index(
        "ix_approvals_denial_history",
        "approvals",
        ["workspace_id", "requested_by", "tool_revision_id", "canonical_args_hash", "decided_at"],
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE approvals, approval_preferences, personal_approval_runs IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM approval_preferences) OR EXISTS (
            SELECT 1 FROM approvals WHERE decision_source='DELEGATED' OR routing_reason IS NOT NULL
        ) OR EXISTS (
            SELECT 1 FROM personal_approval_runs
        ) THEN RAISE EXCEPTION '0057 downgrade refused: personal approval settings or delegated decision history exist';
        END IF; END $$""")
    op.drop_index("ix_approvals_denial_history", table_name="approvals")
    op.drop_constraint("ck_approvals_decision_source", "approvals", type_="check")
    op.drop_column("approvals", "routing_reason")
    op.drop_column("approvals", "decision_source")
    op.drop_table("personal_approval_runs")
    op.drop_table("approval_preferences")
