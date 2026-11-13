"""Track ticket revisions and isolate controlled evaluation customers."""

import sqlalchemy as sa
from alembic import op

revision = "0055_support_records"
down_revision = "0054_memory_provider_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_tickets_positive_version", "tickets", "version > 0")
    op.add_column(
        "customers",
        sa.Column("is_evaluation_fixture", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Durable episodes record ownership. The earlier standalone fixture writer
    # used all three deterministic fields below, including its configurable prefix.
    op.execute("""
        UPDATE customers c SET is_evaluation_fixture = TRUE
        WHERE EXISTS (
            SELECT 1 FROM evaluation_interactive_episodes e
            WHERE e.workspace_id = c.workspace_id AND e.customer_id = c.id
        ) OR (
            c.customer_ref ~ '-[0-9a-f]{24}$'
            AND c.name = 'AE6B Fixture ' || left(right(c.customer_ref, 24), 8)
            AND c.email = lower(c.customer_ref) || '@example.test'
        )
    """)


def downgrade() -> None:
    op.drop_column("customers", "is_evaluation_fixture")
    op.drop_constraint("ck_tickets_positive_version", "tickets", type_="check")
    op.drop_column("tickets", "version")
