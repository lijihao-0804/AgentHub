"""Add disposable, workspace-scoped document vector cache."""

import sqlalchemy as sa
from alembic import op

revision = "0059_document_vector_cache"
down_revision = "0058_revision_chunking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_vector_cache",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("modality", sa.String(6), primary_key=True),
        sa.Column("encoder_fingerprint", sa.String(64), primary_key=True),
        sa.Column("text_hash", sa.String(64), primary_key=True),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("vector_sha256", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
            name="fk_document_vector_cache_workspace",
        ),
        sa.CheckConstraint(
            "modality IN ('dense', 'sparse')", name="ck_document_vector_cache_modality"
        ),
    )


def downgrade() -> None:
    op.drop_table("document_vector_cache")
