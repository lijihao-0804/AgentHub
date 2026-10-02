"""Retained handoff evidence with separate operational lifecycle."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_handoff_cases"
down_revision = "0030_run_feedback"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "handoff_cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("assignee_id", sa.Uuid()),
        sa.Column("claimed_by", sa.Uuid()),
        sa.Column("closed_by", sa.Uuid()),
        sa.Column("closure_reason", sa.Text()),
        sa.Column("unresolved_items", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("close_request_hash", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_artifact_id"],
            ["artifacts.workspace_id", "artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["claimed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["closed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("source_artifact_id", name="uq_handoff_source"),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ASSIGNED', 'IN_PROGRESS', 'CLOSED')", name="ck_handoff_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_handoff_version"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND assignee_id IS NULL "
            "AND claimed_by IS NULL AND closed_by IS NULL) OR "
            "(status = 'ASSIGNED' AND assignee_id IS NOT NULL "
            "AND claimed_by IS NULL AND closed_by IS NULL) OR "
            "(status = 'IN_PROGRESS' AND assignee_id IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claimed_by = assignee_id AND closed_by IS NULL) OR "
            "(status = 'CLOSED' AND assignee_id IS NOT NULL AND claimed_by IS NOT NULL "
            "AND closed_by IS NOT NULL AND claimed_by = assignee_id AND closed_by = assignee_id "
            "AND closure_reason IS NOT NULL AND close_request_hash IS NOT NULL)",
            name="ck_handoff_state_fields",
        ),
    )
    op.create_index(
        "ix_handoff_workspace_thread",
        "handoff_cases",
        ["workspace_id", "thread_id", "created_at", "id"],
    )


def downgrade():
    op.drop_table("handoff_cases")
