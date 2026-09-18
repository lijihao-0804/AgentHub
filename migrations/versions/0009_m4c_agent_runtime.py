"""Add durable M4-C AgentRun and RunStep history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_m4c_agent_runtime"
down_revision: str | None = "0008_m4b_tool_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()
    op.create_table(
        "agent_runs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("agent_version_id", uuid_type, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RUNNING"),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("final_output", sa.Text()),
        sa.Column("failure_code", sa.String(length=96)),
        sa.Column("model_step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name="fk_agent_runs_agent_version_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_agent_runs_created_by", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name="ck_agent_runs_status"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_agent_runs_workspace_id"),
    )
    op.create_index("ix_agent_runs_workspace_status", "agent_runs", ["workspace_id", "status"])
    op.create_index(
        "ix_agent_runs_workspace_created_at", "agent_runs", ["workspace_id", "created_at"]
    )

    op.create_table(
        "run_steps",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("agent_run_id", uuid_type, nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "safe_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_run_steps_agent_run_workspace",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
            "'OBSERVATION', 'GUARD', 'FINISH')",
            name="ck_run_steps_kind",
        ),
        sa.UniqueConstraint(
            "workspace_id", "agent_run_id", "sequence_number", name="uq_run_steps_sequence"
        ),
    )
    op.create_index(
        "ix_run_steps_workspace_run",
        "run_steps",
        ["workspace_id", "agent_run_id", "sequence_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_steps_workspace_run", table_name="run_steps")
    op.drop_table("run_steps")
    op.drop_index("ix_agent_runs_workspace_created_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_workspace_status", table_name="agent_runs")
    op.drop_table("agent_runs")
