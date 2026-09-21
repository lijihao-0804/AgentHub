"""Persist artifacts: what an agent produced, as opposed to what it said.

``type`` deliberately carries no CHECK constraint. The allowed set is an
application-layer whitelist instead, because otherwise every new artifact kind
would cost a migration — and the whitelist refuses just as hard.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0024_artifacts"
down_revision: str | None = "0023_agent_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "artifacts",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        # Null means a human made this. Not null means it is the record of one
        # run's output, which is why those are not editable afterwards.
        sa.Column("run_id", uuid_type, nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_artifacts_thread_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_artifacts_run_workspace",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_artifacts_created_by", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_artifacts_workspace_id"),
    )
    op.create_index(
        "ix_artifacts_thread_created_at",
        "artifacts",
        ["workspace_id", "thread_id", "created_at", "id"],
    )
    op.create_index("ix_artifacts_thread_type", "artifacts", ["workspace_id", "thread_id", "type"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_thread_type", table_name="artifacts")
    op.drop_index("ix_artifacts_thread_created_at", table_name="artifacts")
    op.drop_table("artifacts")
