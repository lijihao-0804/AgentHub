"""Add durable M7-C experiment execution state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_m7_experiment_execution"
down_revision: str | None = "0016_m7_experiment_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    now = sa.func.now()

    op.add_column(
        "evaluation_experiment_runs",
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "evaluation_experiment_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "evaluation_experiment_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "evaluation_experiment_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_evaluation_experiment_runs_lease",
        "evaluation_experiment_runs",
        ["status", "lease_expires_at", "id"],
    )
    op.create_unique_constraint(
        "uq_evaluation_dataset_items_workspace_id",
        "evaluation_dataset_items",
        ["workspace_id", "id"],
    )

    op.create_table(
        "evaluation_experiment_case_results",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("experiment_run_id", uuid_type, nullable=False),
        sa.Column("experiment_variant_id", uuid_type, nullable=False),
        sa.Column("dataset_item_id", uuid_type, nullable=False),
        sa.Column("repetition_index", sa.Integer(), nullable=False),
        sa.Column("case_execution_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("agent_run_id", uuid_type),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("cost_amount", sa.Numeric(20, 8)),
        sa.Column("cost_currency", sa.String(length=3)),
        sa.Column("failure_code", sa.String(length=96)),
        sa.Column("safe_failure_message", sa.Text()),
        sa.Column("observation", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_case_results_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_case_results_variant_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_item_id"],
            ["evaluation_dataset_items.workspace_id", "evaluation_dataset_items.id"],
            name="fk_evaluation_case_results_dataset_item_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_evaluation_case_results_agent_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_evaluation_case_results_status",
        ),
        sa.CheckConstraint("repetition_index >= 0", name="ck_evaluation_case_results_repetition"),
        sa.UniqueConstraint(
            "workspace_id",
            "experiment_run_id",
            "experiment_variant_id",
            "dataset_item_id",
            "repetition_index",
            name="uq_evaluation_case_results_execution_key",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_case_results_workspace_id"),
    )
    op.create_index(
        "ix_evaluation_case_results_run_status",
        "evaluation_experiment_case_results",
        ["workspace_id", "experiment_run_id", "status"],
    )
    op.create_index(
        "ix_evaluation_case_results_run_order",
        "evaluation_experiment_case_results",
        [
            "workspace_id",
            "experiment_run_id",
            "dataset_item_id",
            "experiment_variant_id",
            "repetition_index",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evaluation_case_results_run_order",
        table_name="evaluation_experiment_case_results",
    )
    op.drop_index(
        "ix_evaluation_case_results_run_status",
        table_name="evaluation_experiment_case_results",
    )
    op.drop_table("evaluation_experiment_case_results")
    op.drop_constraint(
        "uq_evaluation_dataset_items_workspace_id",
        "evaluation_dataset_items",
        type_="unique",
    )
    op.drop_index("ix_evaluation_experiment_runs_lease", table_name="evaluation_experiment_runs")
    op.drop_column("evaluation_experiment_runs", "attempt_count")
    op.drop_column("evaluation_experiment_runs", "heartbeat_at")
    op.drop_column("evaluation_experiment_runs", "lease_expires_at")
    op.drop_column("evaluation_experiment_runs", "lease_owner")
