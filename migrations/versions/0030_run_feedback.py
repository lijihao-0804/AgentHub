"""Human feedback, reviewed corrections and atomic regression provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0030_run_feedback"
down_revision = "0029_set_null_columns"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid()),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("client_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("content_revision", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("corrected_answer", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("reviewed_by", sa.Uuid()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_comment", sa.Text()),
        sa.Column("imported_version_id", sa.Uuid()),
        sa.Column("import_request_hash", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "imported_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "workspace_id", "created_by", "client_key", name="uq_feedback_submission"
        ),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED')", name="ck_feedback_status"
        ),
        sa.CheckConstraint(
            "category IN ('FACTUAL', 'RETRIEVAL', 'TOOL', 'LATENCY', 'EXPRESSION', 'OTHER')",
            name="ck_feedback_category",
        ),
    )
    op.create_index(
        "ix_feedback_workspace_run", "run_feedback", ["workspace_id", "run_id", "created_at"]
    )


def downgrade():
    op.drop_table("run_feedback")
