"""Partition active agent memory by its optional user owner."""

import sqlalchemy as sa
from alembic import op

revision = "0060_private_memory"
down_revision = "0059_document_vector_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_memories", sa.Column("owner_user_id", sa.Uuid()))
    op.drop_index("uq_workspace_memories_active_hash", table_name="workspace_memories")
    for name, columns, predicate in (
        ("active", [], "IS NULL"),
        ("private", ["owner_user_id"], "IS NOT NULL"),
    ):
        op.create_index(
            f"uq_workspace_memories_{name}_hash",
            "workspace_memories",
            ["workspace_id", "agent_id", *columns, "content_hash"],
            unique=True,
            postgresql_where=sa.text(f"status = 'ACTIVE' AND owner_user_id {predicate}"),
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM workspace_memories WHERE owner_user_id IS NOT NULL)")
    ):
        raise RuntimeError(
            "Private memories require an explicit retention decision before downgrade."
        )
    for name in ("active", "private"):
        op.drop_index(f"uq_workspace_memories_{name}_hash", table_name="workspace_memories")
    op.create_index(
        "uq_workspace_memories_active_hash",
        "workspace_memories",
        ["workspace_id", "agent_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_column("workspace_memories", "owner_user_id")
