"""Add scoped customer and ticket data for the M4-B read tool."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_m4b_tool_runtime"
down_revision: str | None = "0007_m4a_agent_publish"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "customers",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("customer_ref", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_customers_workspace", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_customers_workspace_id"),
        sa.UniqueConstraint("workspace_id", "customer_ref", name="uq_customers_workspace_ref"),
    )
    op.create_index("ix_customers_workspace_ref", "customers", ["workspace_id", "customer_ref"])

    op.create_table(
        "tickets",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("customer_id", uuid_type, nullable=False),
        sa.Column("ticket_ref", sa.String(length=128), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="OPEN"),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_tickets_workspace", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "customer_id"],
            ["customers.workspace_id", "customers.id"],
            name="fk_tickets_customer_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tickets_workspace_id"),
        sa.UniqueConstraint("workspace_id", "ticket_ref", name="uq_tickets_workspace_ref"),
    )
    op.create_index("ix_tickets_customer", "tickets", ["workspace_id", "customer_id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_customer", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_customers_workspace_ref", table_name="customers")
    op.drop_table("customers")
