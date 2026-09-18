"""Freeze one revision per document in each knowledge snapshot."""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_m3f_snapshot_document_integrity"
down_revision: str | None = "0005_m3f_snapshot_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_snapshot_items_document",
        "knowledge_snapshot_items",
        ["workspace_id", "snapshot_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_snapshot_items_document",
        "knowledge_snapshot_items",
        type_="unique",
    )
