"""Persist M7-D evaluator metrics and paired comparisons."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_m7d_metrics_comparison"
down_revision: str | None = "0018_m7c_review_closure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    now = sa.func.now()
    op.create_table(
        "evaluation_metric_results",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("experiment_run_id", uuid_type, nullable=False),
        sa.Column("experiment_variant_id", uuid_type, nullable=False),
        sa.Column("dimension", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("metric_name", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("value", sa.Numeric(28, 12)),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("numerator", sa.Numeric(28, 12)),
        sa.Column("denominator", sa.Numeric(28, 12)),
        sa.Column("reason", sa.String(length=160)),
        sa.Column("evaluator_version", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("details", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_metrics_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_metrics_variant_workspace",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('AVAILABLE', 'NOT_APPLICABLE', 'NOT_AVAILABLE')",
            name="ck_evaluation_metrics_status",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "experiment_run_id",
            "experiment_variant_id",
            "dimension",
            "category",
            "metric_name",
            name="uq_evaluation_metrics_identity",
        ),
    )
    op.create_index(
        "ix_evaluation_metrics_run_variant",
        "evaluation_metric_results",
        ["workspace_id", "experiment_run_id", "experiment_variant_id"],
    )
    op.create_table(
        "evaluation_experiment_comparisons",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("experiment_run_id", uuid_type, nullable=False),
        sa.Column("baseline_variant_id", uuid_type, nullable=False),
        sa.Column("candidate_variant_id", uuid_type, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "evaluator_versions", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("metrics", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("missing_pairs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_comparisons_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "baseline_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_comparisons_baseline_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "candidate_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_comparisons_candidate_workspace",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('COMPLETE', 'INCOMPLETE', 'NOT_COMPARABLE')",
            name="ck_evaluation_comparisons_status",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "experiment_run_id",
            "baseline_variant_id",
            "candidate_variant_id",
            name="uq_evaluation_comparisons_pair",
        ),
    )
    op.create_index(
        "ix_evaluation_comparisons_run",
        "evaluation_experiment_comparisons",
        ["workspace_id", "experiment_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_comparisons_run", table_name="evaluation_experiment_comparisons")
    op.drop_table("evaluation_experiment_comparisons")
    op.drop_index("ix_evaluation_metrics_run_variant", table_name="evaluation_metric_results")
    op.drop_table("evaluation_metric_results")
