"""Retain complete opt-in thread exchanges for deterministic history replay."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0039_thread_history_transcript"
down_revision = "0038_thread_history_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("thread_history_transcript", JSONB, nullable=True))


def downgrade():
    op.drop_column("agent_runs", "thread_history_transcript")
