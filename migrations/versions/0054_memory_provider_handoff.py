"""Fence memory extraction claims and persist the provider handoff.

Revision ID: 0054_memory_provider_handoff
Revises: 0053_interactive_case_budget
"""

import sqlalchemy as sa
from alembic import op

revision = "0054_memory_provider_handoff"
down_revision = "0053_interactive_case_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing RUNNING rows may have dispatched. Old workers omit these
    # columns too, so neither backfill nor a server default is safe here.
    op.add_column("memory_extraction_jobs", sa.Column("claim_token", sa.Uuid(), nullable=True))
    op.add_column(
        "memory_extraction_jobs", sa.Column("provider_call_possible", sa.Boolean(), nullable=True)
    )
    op.create_check_constraint(
        "ck_memory_extraction_jobs_handoff_claim",
        "memory_extraction_jobs",
        "provider_call_possible IS NULL OR claim_token IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_extraction_jobs_handoff_claim", "memory_extraction_jobs")
    op.drop_column("memory_extraction_jobs", "provider_call_possible")
    op.drop_column("memory_extraction_jobs", "claim_token")
