"""Add M7-A evaluation datasets and immutable pricing snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_m7_evaluation_platform"
down_revision: str | None = "0014_m6_metrics_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "evaluation_datasets",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_evaluation_datasets_workspace", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_evaluation_datasets_created_by", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_datasets_workspace_id"),
    )
    op.create_index(
        "ix_evaluation_datasets_workspace_created",
        "evaluation_datasets",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "evaluation_dataset_versions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("dataset_id", uuid_type, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="DRAFT"),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_id"],
            ["evaluation_datasets.workspace_id", "evaluation_datasets.id"],
            name="fk_evaluation_dataset_versions_dataset_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_dataset_versions_created_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_evaluation_dataset_versions_number_positive"
        ),
        sa.CheckConstraint(
            "schema_version > 0", name="ck_evaluation_dataset_versions_schema_positive"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED')", name="ck_evaluation_dataset_versions_status"
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_evaluation_dataset_versions_workspace_id"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "dataset_id",
            "version_number",
            name="uq_evaluation_dataset_versions_number",
        ),
    )
    op.create_index(
        "ix_evaluation_dataset_versions_dataset_status",
        "evaluation_dataset_versions",
        ["workspace_id", "dataset_id", "status"],
    )

    op.create_table(
        "evaluation_dataset_items",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("dataset_version_id", uuid_type, nullable=False),
        sa.Column("case_key", sa.String(length=200), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("expected", postgresql.JSONB(), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_provenance", postgresql.JSONB(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "dataset_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            name="fk_evaluation_dataset_items_version_workspace",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "split IN ('DEV', 'HOLDOUT')", name="ck_evaluation_dataset_items_split"
        ),
        sa.CheckConstraint(
            "category IN ('RETRIEVAL', 'KNOWLEDGE_QA', 'TOOL', 'NO_ANSWER', 'APPROVAL', 'MULTI_STEP', 'FAILURE')",
            name="ck_evaluation_dataset_items_category",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_evaluation_dataset_items_ordinal_nonnegative"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "dataset_version_id",
            "case_key",
            name="uq_evaluation_dataset_items_case_key",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "dataset_version_id",
            "ordinal",
            name="uq_evaluation_dataset_items_ordinal",
        ),
    )
    op.create_index(
        "ix_evaluation_dataset_items_version_split",
        "evaluation_dataset_items",
        ["workspace_id", "dataset_version_id", "split"],
    )

    op.create_table(
        "pricing_snapshots",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("input_price_per_1m", sa.Numeric(20, 8), nullable=False),
        sa.Column("output_price_per_1m", sa.Numeric(20, 8), nullable=False),
        sa.Column("cached_input_price_per_1m", sa.Numeric(20, 8)),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_pricing_snapshots_workspace", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_pricing_snapshots_created_by", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_pricing_snapshots_workspace_id"),
    )
    op.create_index(
        "ix_pricing_snapshots_workspace_effective",
        "pricing_snapshots",
        ["workspace_id", "effective_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pricing_snapshots_workspace_effective", table_name="pricing_snapshots")
    op.drop_table("pricing_snapshots")
    op.drop_index(
        "ix_evaluation_dataset_items_version_split", table_name="evaluation_dataset_items"
    )
    op.drop_table("evaluation_dataset_items")
    op.drop_index(
        "ix_evaluation_dataset_versions_dataset_status",
        table_name="evaluation_dataset_versions",
    )
    op.drop_table("evaluation_dataset_versions")
    op.drop_index("ix_evaluation_datasets_workspace_created", table_name="evaluation_datasets")
    op.drop_table("evaluation_datasets")
