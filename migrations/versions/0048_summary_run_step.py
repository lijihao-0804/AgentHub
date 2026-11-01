"""Allow summary admission in the durable Run step timeline.

Revision ID: 0048_summary_run_step
Revises: 0047_summary_checkpoint_binding
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_summary_run_step"
down_revision = "0047_summary_checkpoint_binding"
branch_labels = None
depends_on = None

_RUN_STEP_KINDS = (
    "'PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
    "'OBSERVATION', 'APPROVAL_WAIT', 'ACTION', 'RECONCILIATION', 'SUMMARY', "
    "'GUARD', 'FINISH'"
)
_PREVIOUS_RUN_STEP_KINDS = (
    "'PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
    "'OBSERVATION', 'APPROVAL_WAIT', 'ACTION', 'RECONCILIATION', 'GUARD', 'FINISH'"
)


def upgrade() -> None:
    op.drop_constraint("ck_run_steps_kind", "run_steps", type_="check")
    op.create_check_constraint(
        "ck_run_steps_kind",
        "run_steps",
        sa.text(f"kind IN ({_RUN_STEP_KINDS})"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_steps_kind", "run_steps", type_="check")
    op.create_check_constraint(
        "ck_run_steps_kind",
        "run_steps",
        sa.text(f"kind IN ({_PREVIOUS_RUN_STEP_KINDS})"),
    )
