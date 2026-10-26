"""Bind resume dispatch to the active interrupt; retain event-log failures."""

import sqlalchemy as sa
from alembic import op

revision = "0037_run_resume_cursor"
down_revision = "0036_turn_memory_artifact"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("waiting_approval_id", sa.Uuid()))
    op.add_column(
        "agent_runs",
        sa.Column("event_log_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Historical best-effort logs have no proof of completeness. A later
    # strict resume segment must not turn that historical uncertainty into a
    # claim that the whole log was recorded.
    op.execute("UPDATE agent_runs SET event_log_failed = true")


def downgrade():
    op.drop_column("agent_runs", "event_log_failed")
    op.drop_column("agent_runs", "waiting_approval_id")
