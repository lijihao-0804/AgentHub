"""Persist threads: the container a user's continuous work lives in.

A thread is not a run and does not become one. The only thing this migration
adds to ``agent_runs`` is a nullable back-pointer, so a Playground run — which
has no thread — is stored exactly as it was before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0023_agent_threads"
down_revision: str | None = "0022_mcp_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "agent_threads",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("agent_id", uuid_type, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_threads_agent_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_agent_threads_created_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_agent_threads_workspace_id"),
    )
    op.create_index(
        "ix_agent_threads_workspace_updated_at",
        "agent_threads",
        ["workspace_id", "updated_at", "id"],
    )
    op.create_index(
        "ix_agent_threads_workspace_agent",
        "agent_threads",
        ["workspace_id", "agent_id", "updated_at"],
    )

    op.create_table(
        "thread_turns",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("user_input", sa.Text(), nullable=False),
        # Null for the instant between reserving the turn and creating its run.
        sa.Column("agent_run_id", uuid_type, nullable=True),
        # Lets a retried submission find the turn it already created instead of
        # turning one question into two runs.
        sa.Column("client_token", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_thread_turns_thread_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_thread_turns_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_thread_turns_workspace_id"),
        sa.UniqueConstraint("thread_id", "sequence", name="uq_thread_turns_sequence"),
        sa.UniqueConstraint("thread_id", "client_token", name="uq_thread_turns_client_token"),
    )
    op.create_index(
        "ix_thread_turns_thread_sequence", "thread_turns", ["workspace_id", "thread_id", "sequence"]
    )

    # The one change to the execution layer. Nullable on purpose: every run that
    # already exists, and every future single-shot Playground run, keeps NULL
    # here and is unaffected by everything above.
    op.add_column("agent_runs", sa.Column("thread_id", uuid_type, nullable=True))
    op.create_foreign_key(
        "fk_agent_runs_thread_workspace",
        "agent_runs",
        "agent_threads",
        ["workspace_id", "thread_id"],
        ["workspace_id", "id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_runs_thread", "agent_runs", ["workspace_id", "thread_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_thread", table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_thread_workspace", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "thread_id")
    op.drop_index("ix_thread_turns_thread_sequence", table_name="thread_turns")
    op.drop_table("thread_turns")
    op.drop_index("ix_agent_threads_workspace_agent", table_name="agent_threads")
    op.drop_index("ix_agent_threads_workspace_updated_at", table_name="agent_threads")
    op.drop_table("agent_threads")
