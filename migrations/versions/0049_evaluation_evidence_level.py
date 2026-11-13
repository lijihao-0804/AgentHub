"""Add evidence level to evaluation experiments and runs.

Revision ID: 0049_evaluation_evidence_level
Revises: 0048_summary_run_step
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_evaluation_evidence_level"
down_revision = "0048_summary_run_step"
branch_labels = None
depends_on = None

_LEVELS = (
    "'DETERMINISTIC_CONFORMANCE', 'CONTROLLED_SIMULATION', "
    "'REAL_PROVIDER', 'PRODUCTION_SAMPLE'"
)


def upgrade() -> None:
    op.add_column(
        "evaluation_experiments",
        sa.Column(
            "evidence_level",
            sa.String(length=32),
            nullable=False,
            server_default="DETERMINISTIC_CONFORMANCE",
        ),
    )
    op.add_column(
        "evaluation_experiment_runs",
        sa.Column(
            "evidence_level",
            sa.String(length=32),
            nullable=False,
            server_default="DETERMINISTIC_CONFORMANCE",
        ),
    )
    op.create_check_constraint(
        "ck_evaluation_experiments_evidence_level",
        "evaluation_experiments",
        sa.text(f"evidence_level IN ({_LEVELS})"),
    )
    op.create_check_constraint(
        "ck_evaluation_experiment_runs_evidence_level",
        "evaluation_experiment_runs",
        sa.text(f"evidence_level IN ({_LEVELS})"),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_evaluation_experiment_runs_evidence_level",
        "evaluation_experiment_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_experiments_evidence_level",
        "evaluation_experiments",
        type_="check",
    )
    op.drop_column("evaluation_experiment_runs", "evidence_level")
    op.drop_column("evaluation_experiments", "evidence_level")
