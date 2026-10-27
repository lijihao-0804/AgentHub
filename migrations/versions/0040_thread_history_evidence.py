"""Bind private evidence provenance to an opt-in historical transcript."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0040_thread_history_evidence"
down_revision = "0039_thread_history_transcript"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("thread_history_evidence_registry", JSONB, nullable=True))


def downgrade():
    op.drop_column("agent_runs", "thread_history_evidence_registry")
