"""Durable run ownership, approval resume outbox and event completeness."""

import sqlalchemy as sa
from alembic import op

revision = "0035_run_execution_lease"
down_revision = "0034_wiki_native_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("execution_owner", sa.String(64)))
    op.add_column("agent_runs", sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "agent_runs",
        sa.Column("resume_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("agent_runs", sa.Column("reconciled_at", sa.DateTime(timezone=True)))
    op.add_column(
        "agent_runs",
        sa.Column("event_log_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Old workers did not persist execution ownership. Their interrupted runs
    # cannot be confused with the new, safe-to-start RUNNING/owner=NULL draft.
    # Deployment must drain/stop old workers before applying these migrations.
    op.execute("""
        UPDATE agent_runs AS run
        SET status = 'NEEDS_ATTENTION', failure_code = 'LEGACY_RUN_OWNERSHIP_UNKNOWN',
            completed_at = now()
        WHERE run.status IN ('RUNNING', 'CANCEL_REQUESTED')
           OR EXISTS (
               SELECT 1 FROM approvals AS approval
               WHERE approval.workspace_id = run.workspace_id AND approval.run_id = run.id
                 AND approval.execution_status IN ('CLAIMED', 'UNKNOWN_OUTCOME')
           )
    """)
    op.execute("""
        UPDATE approvals AS approval
        SET execution_status = 'UNKNOWN_OUTCOME', failure_code = 'ACTION_OUTCOME_UNKNOWN',
            executed_at = now()
        FROM agent_runs AS run
        WHERE approval.workspace_id = run.workspace_id AND approval.run_id = run.id
          AND run.failure_code = 'LEGACY_RUN_OWNERSHIP_UNKNOWN'
          AND approval.execution_status = 'CLAIMED'
    """)


def downgrade():
    for name in (
        "event_log_complete",
        "reconciled_at",
        "resume_pending",
        "execution_lease_expires_at",
        "execution_owner",
    ):
        op.drop_column("agent_runs", name)
