"""Add M5 durable approvals and action idempotency storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_m5_approval_runtime"
down_revision: str | None = "0011_h2_agent_run_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'FAILED', "
        "'NEEDS_ATTENTION', 'CANCEL_REQUESTED', 'CANCELLED')",
    )
    op.drop_constraint("ck_run_steps_kind", "run_steps", type_="check")
    op.create_check_constraint(
        "ck_run_steps_kind",
        "run_steps",
        "kind IN ('PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
        "'OBSERVATION', 'APPROVAL_WAIT', 'ACTION', 'RECONCILIATION', 'GUARD', 'FINISH')",
    )

    op.add_column("tickets", sa.Column("idempotency_key", sa.String(length=128)))
    op.create_unique_constraint(
        "uq_tickets_workspace_idempotency", "tickets", ["workspace_id", "idempotency_key"]
    )

    op.create_table(
        "approvals",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("agent_version_id", uuid_type, nullable=False),
        sa.Column("logical_action_id", sa.String(length=128), nullable=False),
        sa.Column("tool_revision_id", uuid_type),
        sa.Column("tool_identity", sa.String(length=128), nullable=False),
        sa.Column("canonical_arguments", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_args_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "decision_status", sa.String(length=16), nullable=False, server_default="PENDING"
        ),
        sa.Column(
            "execution_status", sa.String(length=24), nullable=False, server_default="NOT_STARTED"
        ),
        sa.Column("requested_by", uuid_type, nullable=False),
        sa.Column("decided_by", uuid_type),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(length=96)),
        sa.Column("safe_failure_message", sa.Text()),
        sa.Column("safe_result", postgresql.JSONB()),
        sa.Column("execution_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_approvals_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name="fk_approvals_agent_version_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "tool_revision_id"],
            ["tool_revisions.workspace_id", "tool_revisions.id"],
            name="fk_approvals_tool_revision_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name="fk_approvals_requested_by", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name="fk_approvals_decided_by", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "decision_status IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED', 'CANCELLED')",
            name="ck_approvals_decision_status",
        ),
        sa.CheckConstraint(
            "execution_status IN ('NOT_STARTED', 'CLAIMED', 'SUCCEEDED', 'FAILED', "
            "'UNKNOWN_OUTCOME')",
            name="ck_approvals_execution_status",
        ),
        sa.UniqueConstraint(
            "workspace_id", "logical_action_id", name="uq_approvals_workspace_logical_action"
        ),
        sa.UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_approvals_workspace_idempotency"
        ),
    )
    op.create_index(
        "ix_approvals_workspace_decision", "approvals", ["workspace_id", "decision_status"]
    )
    op.create_index("ix_approvals_workspace_run", "approvals", ["workspace_id", "run_id"])


def downgrade() -> None:
    op.drop_index("ix_approvals_workspace_run", table_name="approvals")
    op.drop_index("ix_approvals_workspace_decision", table_name="approvals")
    op.drop_table("approvals")
    op.drop_constraint("uq_tickets_workspace_idempotency", "tickets", type_="unique")
    op.drop_column("tickets", "idempotency_key")

    op.drop_constraint("ck_run_steps_kind", "run_steps", type_="check")
    op.create_check_constraint(
        "ck_run_steps_kind",
        "run_steps",
        "kind IN ('PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
        "'OBSERVATION', 'GUARD', 'FINISH')",
    )
    op.drop_constraint("ck_agent_runs_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_status",
        "agent_runs",
        "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
    )
