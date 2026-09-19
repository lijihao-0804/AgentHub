"""Freeze M7-D metric snapshots and comparisons."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_m7d_review_closure"
down_revision: str | None = "0019_m7d_metrics_comparison"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    now = sa.func.now()

    op.create_table(
        "evaluation_metric_snapshots",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("experiment_run_id", uuid_type, nullable=False),
        sa.Column("evaluator_manifest", json_type, nullable=False),
        sa.Column("evaluator_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("case_result_set_hash", sa.String(length=64), nullable=False),
        sa.Column("metrics_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_metric_snapshots_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_evaluation_metric_snapshots_created_by"
        ),
        sa.UniqueConstraint(
            "workspace_id", "experiment_run_id", name="uq_evaluation_metric_snapshots_run"
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_metric_snapshots_workspace_id"
        ),
    )
    op.create_index(
        "ix_evaluation_metric_snapshots_workspace_created",
        "evaluation_metric_snapshots",
        ["workspace_id", "created_at"],
    )

    op.alter_column(
        "evaluation_metric_results",
        "experiment_variant_id",
        existing_type=uuid_type,
        nullable=True,
    )
    for name, column_type in (
        ("metric_snapshot_id", uuid_type),
        ("metric_snapshot_hash", sa.String(length=64)),
        ("baseline_variant_hash", sa.String(length=64)),
        ("candidate_variant_hash", sa.String(length=64)),
        ("evaluator_manifest_hash", sa.String(length=64)),
        ("comparison_hash", sa.String(length=64)),
        ("created_by", uuid_type),
    ):
        op.add_column("evaluation_experiment_comparisons", sa.Column(name, column_type))
    op.add_column(
        "evaluation_experiment_comparisons",
        sa.Column("paired_pairs", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_evaluation_comparisons_metric_snapshot_workspace",
        "evaluation_experiment_comparisons",
        "evaluation_metric_snapshots",
        ["workspace_id", "metric_snapshot_id"],
        ["workspace_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_evaluation_comparisons_created_by",
        "evaluation_experiment_comparisons",
        "users",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_evaluation_comparisons_created_by",
        "evaluation_experiment_comparisons",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_evaluation_comparisons_metric_snapshot_workspace",
        "evaluation_experiment_comparisons",
        type_="foreignkey",
    )
    for name in (
        "paired_pairs",
        "created_by",
        "comparison_hash",
        "evaluator_manifest_hash",
        "candidate_variant_hash",
        "baseline_variant_hash",
        "metric_snapshot_hash",
        "metric_snapshot_id",
    ):
        op.drop_column("evaluation_experiment_comparisons", name)
    uuid_type = postgresql.UUID(as_uuid=True)
    op.alter_column(
        "evaluation_metric_results",
        "experiment_variant_id",
        existing_type=uuid_type,
        nullable=False,
    )
    op.drop_index(
        "ix_evaluation_metric_snapshots_workspace_created",
        table_name="evaluation_metric_snapshots",
    )
    op.drop_table("evaluation_metric_snapshots")
