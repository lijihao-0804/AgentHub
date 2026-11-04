"""Index the bounded Memory extraction recovery sweep.

Revision ID: 0052_memory_recovery_index
Revises: 0051_interactive_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_memory_recovery_index"
down_revision = "0051_interactive_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_memory_extraction_jobs_recovery",
        "memory_extraction_jobs",
        ["updated_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("ix_memory_extraction_jobs_recovery", table_name="memory_extraction_jobs")
