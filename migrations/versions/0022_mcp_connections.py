"""Persist workspace-managed remote MCP connections."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0022_mcp_connections"
down_revision: str | None = "0021_m7ef_ablation_release_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "mcp_connections",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("auth_type", sa.String(length=16), nullable=False),
        # The bearer token exists only as ciphertext; there is deliberately no
        # plaintext column for it to be written to by mistake.
        sa.Column("secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("secret_version", sa.Integer(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_mcp_connections_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_mcp_connections_created_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_mcp_connections_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_mcp_connections_workspace_name"),
        sa.CheckConstraint(
            "auth_type IN ('NONE', 'BEARER')", name="ck_mcp_connections_auth_type"
        ),
        sa.CheckConstraint(
            "(auth_type = 'BEARER') = (secret_ciphertext IS NOT NULL)",
            name="ck_mcp_connections_secret_matches_auth_type",
        ),
    )
    op.create_index("ix_mcp_connections_workspace_id", "mcp_connections", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_connections_workspace_id", table_name="mcp_connections")
    op.drop_table("mcp_connections")
