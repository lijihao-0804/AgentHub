"""Persist the opt-in deterministic history source snapshot for each run."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0038_thread_history_snapshot"
down_revision = "0037_run_resume_cursor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_runs", sa.Column("effective_thread_history_snapshot", JSONB, nullable=True)
    )


def downgrade():
    op.drop_column("agent_runs", "effective_thread_history_snapshot")
