"""Create the M0 semantic baseline marker.

Revision ID: 0001_m0_semantic_baseline
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_m0_semantic_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agenthub_schema_meta",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.String(length=255), nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "agenthub_schema_meta",
            sa.column("key", sa.String),
            sa.column("value", sa.String),
        ),
        [
            {"key": "semantic_contract_version", "value": "3.1"},
            {"key": "api_contract_version", "value": "v1"},
        ],
    )


def downgrade() -> None:
    op.drop_table("agenthub_schema_meta")
