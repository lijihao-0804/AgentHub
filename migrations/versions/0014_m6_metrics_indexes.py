"""Add bounded workspace metrics indexes for M6-B queries."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_m6_metrics_indexes"
down_revision: str | None = "0013_m6_operational_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_runs_workspace_started_at",
        "agent_runs",
        ["workspace_id", "started_at", "id"],
    )
    op.create_index(
        "ix_agent_runs_workspace_status_started",
        "agent_runs",
        ["workspace_id", "status", "started_at"],
    )
    op.create_index(
        "ix_agent_runs_workspace_version_started",
        "agent_runs",
        ["workspace_id", "agent_version_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_workspace_version_started", table_name="agent_runs")
    op.drop_index("ix_agent_runs_workspace_status_started", table_name="agent_runs")
    op.drop_index("ix_agent_runs_workspace_started_at", table_name="agent_runs")
