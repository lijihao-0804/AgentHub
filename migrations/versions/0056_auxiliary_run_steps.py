"""Admit existing evidence-selection and auxiliary billing timeline events.

Downgrade refuses to discard or reinterpret retained new-kind evidence.
"""

import sqlalchemy as sa
from alembic import op

revision = "0056_auxiliary_run_steps"
down_revision = "0055_support_records"
branch_labels = None
depends_on = None

_PREVIOUS_KINDS = (
    "'PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
    "'OBSERVATION', 'APPROVAL_WAIT', 'ACTION', 'RECONCILIATION', 'SUMMARY', "
    "'GUARD', 'FINISH'"
)
_KINDS = _PREVIOUS_KINDS + ", 'EVIDENCE_SELECTION', 'AUXILIARY_MODEL'"


def _replace_constraint(kinds: str) -> None:
    op.drop_constraint("ck_run_steps_kind", "run_steps", type_="check")
    op.create_check_constraint("ck_run_steps_kind", "run_steps", sa.text(f"kind IN ({kinds})"))


def upgrade() -> None:
    _replace_constraint(_KINDS)


def downgrade() -> None:
    # Hold the lock through the check and DDL so a concurrent writer cannot
    # introduce a new-kind row between admission checking and constraint swap.
    # SQL also preserves the guard in Alembic offline-generated migrations.
    op.execute("LOCK TABLE run_steps IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM run_steps
                WHERE kind IN ('EVIDENCE_SELECTION', 'AUXILIARY_MODEL')
            ) THEN
                RAISE EXCEPTION
                    '0056 downgrade refused: retained auxiliary run steps require preservation';
            END IF;
        END $$;
    """)
    _replace_constraint(_PREVIOUS_KINDS)
