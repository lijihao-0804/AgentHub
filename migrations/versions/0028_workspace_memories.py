"""Long-term agent memory, plus the per-run snapshot that makes it replayable.

Two things arrive together on purpose.

``workspace_memories`` is what an agent keeps after a thread ends: short
statements scoped to ``(workspace_id, agent_id)``, with provenance back to the
thread and run that taught them. Nothing is ever hard-deleted -- a contradicted
statement is SUPERSEDED and points at its replacement, a human override is
INVALIDATED -- because "why does it still remember that" and "why did it stop"
both have to be answerable after the fact.

``agent_runs.effective_memory_snapshot`` is the reason the first table is safe
to have. Memory is mutable and runs are replayable; without freezing the
selection at the first PREPARE, a run resumed after an approval could see a
memory that did not exist when it started, and an evaluation A/B comparing
memory-on against memory-off would not be comparing two runs of the same thing.
The column defaults to ``'{}'`` and stays empty for every agent that has not
turned the feature on, which is all of them until someone does.

The unique index on ``content_hash`` is partial on ``status = 'ACTIVE'``:
deduplication applies to what the agent currently believes. A fact that was
superseded a month ago may legitimately be learned again, and that insert has
to be allowed to succeed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0028_workspace_memories"
down_revision: str | None = "0027_agent_run_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_memories",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("source_run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="FACT"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTIVE"),
        sa.Column("superseded_by_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("salience", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_memories_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_workspace_memories_agent_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_workspace_memories_thread_workspace",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_workspace_memories_run_workspace",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "kind IN ('FACT', 'PREFERENCE', 'DECISION', 'CONSTRAINT')",
            name="ck_workspace_memories_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUPERSEDED', 'INVALIDATED')",
            name="ck_workspace_memories_status",
        ),
        sa.CheckConstraint("salience > 0", name="ck_workspace_memories_salience_positive"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_workspace_memories_workspace_id"),
    )
    op.create_index(
        "uq_workspace_memories_active_hash",
        "workspace_memories",
        ["workspace_id", "agent_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_workspace_memories_agent",
        "workspace_memories",
        ["workspace_id", "agent_id", "status"],
    )
    op.create_index(
        "ix_workspace_memories_run",
        "workspace_memories",
        ["workspace_id", "source_run_id"],
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "effective_memory_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "effective_memory_snapshot")
    op.drop_index("ix_workspace_memories_run", table_name="workspace_memories")
    op.drop_index("ix_workspace_memories_agent", table_name="workspace_memories")
    op.drop_index("uq_workspace_memories_active_hash", table_name="workspace_memories")
    op.drop_table("workspace_memories")
