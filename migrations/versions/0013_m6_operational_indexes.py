"""Add bounded reconciliation indexes for M6 operational scans."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_m6_operational_indexes"
down_revision: str | None = "0012_m5_approval_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_runs_reconciliation",
        "agent_runs",
        ["status", "started_at", "id"],
    )
    op.create_index(
        "ix_approvals_workspace_execution",
        "approvals",
        ["workspace_id", "execution_status"],
    )
    op.create_index(
        "ix_approvals_workspace_pending_expiry",
        "approvals",
        ["workspace_id", "decision_status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_workspace_pending_expiry", table_name="approvals")
    op.drop_index("ix_approvals_workspace_execution", table_name="approvals")
    op.drop_index("ix_agent_runs_reconciliation", table_name="agent_runs")
