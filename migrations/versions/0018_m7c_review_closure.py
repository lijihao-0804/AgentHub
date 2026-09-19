"""Harden M7-C execution fencing and observed-agent projections."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_m7c_review_closure"
down_revision: str | None = "0017_m7_experiment_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_experiment_runs",
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "evaluation_experiment_case_results",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "evaluation_experiment_case_results",
        sa.Column("lease_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "evaluation_experiment_case_results",
        sa.Column("observed_agent_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "evaluation_experiment_case_results",
        sa.Column("observed_agent_failure_code", sa.String(length=96), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_experiment_case_results", "observed_agent_failure_code")
    op.drop_column("evaluation_experiment_case_results", "observed_agent_status")
    op.drop_column("evaluation_experiment_case_results", "lease_generation")
    op.drop_column("evaluation_experiment_case_results", "lease_owner")
    op.drop_column("evaluation_experiment_runs", "lease_generation")
