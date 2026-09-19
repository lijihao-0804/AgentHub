"""Add M7-B reproducible experiment definitions, runs and holdout exposure records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_m7_experiment_persistence"
down_revision: str | None = "0015_m7_evaluation_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    now = sa.func.now()

    op.create_table(
        "evaluation_experiments",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("dataset_version_id", uuid_type, nullable=False),
        sa.Column("dataset_content_hash", sa.String(length=64), nullable=False),
        sa.Column("dataset_schema_version", sa.Integer(), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="DRAFT"),
        sa.Column("build_sha", sa.String(length=64), nullable=False),
        sa.Column("evaluator_manifest", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("spec_json", json_type),
        sa.Column("spec_hash", sa.String(length=64)),
        sa.Column("holdout_exposure_index", sa.Integer()),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evaluation_experiments_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_experiments_dataset_version_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_evaluation_experiments_created_by", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "split IN ('DEV', 'HOLDOUT')", name="ck_evaluation_experiments_split"
        ),
        sa.CheckConstraint(
            "purpose IN ('DEVELOPMENT', 'HOLDOUT_VALIDATION', 'RELEASE_GATE')",
            name="ck_evaluation_experiments_purpose",
        ),
        sa.CheckConstraint(
            "(purpose = 'DEVELOPMENT' AND split = 'DEV') OR "
            "(purpose IN ('HOLDOUT_VALIDATION', 'RELEASE_GATE') AND split = 'HOLDOUT')",
            name="ck_evaluation_experiments_purpose_split",
        ),
        sa.CheckConstraint(
            "repetitions BETWEEN 1 AND 5", name="ck_evaluation_experiments_repetitions"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'READY')", name="ck_evaluation_experiments_status"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_experiments_workspace_id"),
    )
    op.create_index(
        "ix_evaluation_experiments_workspace_created",
        "evaluation_experiments",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_evaluation_experiments_workspace_dataset",
        "evaluation_experiments",
        ["workspace_id", "dataset_version_id"],
    )

    op.create_table(
        "evaluation_experiment_variants",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("experiment_id", uuid_type, nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("agent_version_id", uuid_type, nullable=False),
        sa.Column("resolved_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("pricing_snapshot_id", uuid_type, nullable=False),
        sa.Column("pricing_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_knowledge_snapshots", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("variant_metadata", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("variant_hash", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["evaluation_experiments.workspace_id", "evaluation_experiments.id"],
            name="fk_evaluation_experiment_variants_experiment_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name="fk_evaluation_experiment_variants_agent_version_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pricing_snapshot_id"],
            ["pricing_snapshots.workspace_id", "pricing_snapshots.id"],
            name="fk_evaluation_experiment_variants_pricing_workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ordinal >= 0 AND ordinal < 5", name="ck_evaluation_experiment_variants_ordinal"
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_experiment_variants_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id", "experiment_id", "label", name="uq_evaluation_experiment_variants_label"
        ),
        sa.UniqueConstraint(
            "workspace_id", "experiment_id", "ordinal", name="uq_evaluation_experiment_variants_ordinal"
        ),
    )
    op.create_index(
        "ix_evaluation_experiment_variants_experiment_ordinal",
        "evaluation_experiment_variants",
        ["workspace_id", "experiment_id", "ordinal"],
    )

    op.create_table(
        "evaluation_experiment_runs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("experiment_id", uuid_type, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="QUEUED"),
        sa.Column("git_commit", sa.String(length=64), nullable=False),
        sa.Column("dataset_version_id", uuid_type, nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("experiment_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(length=96)),
        sa.Column("safe_failure_message", sa.Text()),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["evaluation_experiments.workspace_id", "evaluation_experiments.id"],
            name="fk_evaluation_experiment_runs_experiment_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_experiment_runs_dataset_version_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_experiment_runs_created_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCEL_REQUESTED', 'CANCELLED')",
            name="ck_evaluation_experiment_runs_status",
        ),
        sa.CheckConstraint(
            "split IN ('DEV', 'HOLDOUT')", name="ck_evaluation_experiment_runs_split"
        ),
        sa.CheckConstraint(
            "purpose IN ('DEVELOPMENT', 'HOLDOUT_VALIDATION', 'RELEASE_GATE')",
            name="ck_evaluation_experiment_runs_purpose",
        ),
        sa.CheckConstraint(
            "repetitions BETWEEN 1 AND 5", name="ck_evaluation_experiment_runs_repetitions"
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_experiment_runs_workspace_id"
        ),
    )
    op.create_index(
        "ix_evaluation_experiment_runs_experiment_created",
        "evaluation_experiment_runs",
        ["workspace_id", "experiment_id", "created_at"],
    )
    op.create_index(
        "ix_evaluation_experiment_runs_workspace_status",
        "evaluation_experiment_runs",
        ["workspace_id", "status"],
    )

    op.create_table(
        "evaluation_experiment_holdout_exposures",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("dataset_version_id", uuid_type, nullable=False),
        sa.Column("experiment_id", uuid_type, nullable=False),
        sa.Column("experiment_run_id", uuid_type),
        sa.Column("exposure_index", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_holdout_exposures_dataset_version_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_id"],
            ["evaluation_experiments.workspace_id", "evaluation_experiments.id"],
            name="fk_evaluation_holdout_exposures_experiment_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_holdout_exposures_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_holdout_exposures_created_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "exposure_index > 0", name="ck_evaluation_holdout_exposures_index"
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_holdout_exposures_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "dataset_version_id",
            "exposure_index",
            name="uq_evaluation_holdout_exposures_dataset_index",
        ),
    )
    op.create_index(
        "ix_evaluation_holdout_exposures_dataset_created",
        "evaluation_experiment_holdout_exposures",
        ["workspace_id", "dataset_version_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evaluation_holdout_exposures_dataset_created",
        table_name="evaluation_experiment_holdout_exposures",
    )
    op.drop_table("evaluation_experiment_holdout_exposures")
    op.drop_index(
        "ix_evaluation_experiment_runs_workspace_status",
        table_name="evaluation_experiment_runs",
    )
    op.drop_index(
        "ix_evaluation_experiment_runs_experiment_created",
        table_name="evaluation_experiment_runs",
    )
    op.drop_table("evaluation_experiment_runs")
    op.drop_index(
        "ix_evaluation_experiment_variants_experiment_ordinal",
        table_name="evaluation_experiment_variants",
    )
    op.drop_table("evaluation_experiment_variants")
    op.drop_index(
        "ix_evaluation_experiments_workspace_dataset",
        table_name="evaluation_experiments",
    )
    op.drop_index(
        "ix_evaluation_experiments_workspace_created",
        table_name="evaluation_experiments",
    )
    op.drop_table("evaluation_experiments")
