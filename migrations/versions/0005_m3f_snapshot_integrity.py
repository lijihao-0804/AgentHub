"""Harden snapshot and revision composite integrity."""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_m3f_snapshot_integrity"
down_revision: str | None = "0004_m3_knowledge_hub"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_document_revisions_workspace_kb_document_id",
        "document_revisions",
        ["workspace_id", "knowledge_base_id", "document_id", "id"],
    )
    op.create_unique_constraint(
        "uq_knowledge_snapshots_workspace_kb_id",
        "knowledge_snapshots",
        ["workspace_id", "knowledge_base_id", "id"],
    )
    op.drop_constraint(
        "fk_snapshot_items_snapshot_workspace",
        "knowledge_snapshot_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_snapshot_items_revision_workspace",
        "knowledge_snapshot_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_snapshot_items_snapshot_workspace_kb",
        "knowledge_snapshot_items",
        "knowledge_snapshots",
        ["workspace_id", "knowledge_base_id", "snapshot_id"],
        ["workspace_id", "knowledge_base_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_snapshot_items_revision_document_workspace",
        "knowledge_snapshot_items",
        "document_revisions",
        ["workspace_id", "knowledge_base_id", "document_id", "document_revision_id"],
        ["workspace_id", "knowledge_base_id", "document_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_snapshot_items_revision_document_workspace",
        "knowledge_snapshot_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_snapshot_items_snapshot_workspace_kb",
        "knowledge_snapshot_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_snapshot_items_revision_workspace",
        "knowledge_snapshot_items",
        "document_revisions",
        ["workspace_id", "knowledge_base_id", "document_revision_id"],
        ["workspace_id", "knowledge_base_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_snapshot_items_snapshot_workspace",
        "knowledge_snapshot_items",
        "knowledge_snapshots",
        ["workspace_id", "snapshot_id"],
        ["workspace_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_knowledge_snapshots_workspace_kb_id",
        "knowledge_snapshots",
        type_="unique",
    )
    op.drop_constraint(
        "uq_document_revisions_workspace_kb_document_id",
        "document_revisions",
        type_="unique",
    )
