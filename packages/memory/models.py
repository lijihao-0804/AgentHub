"""Persistence for long-term agent memory.

One row is one short statement an agent learned about the work it does for a
workspace: a preference, a constraint, a decision. It is scoped to
``(workspace_id, agent_id)`` -- never to a thread -- because outliving the
thread is the entire point; the thread it was learned in is kept only as
provenance.

Nothing here is ever hard-deleted. "Why does it still remember that?" and "why
did it stop remembering that?" are both questions an operator has to be able to
answer, and a DELETE answers neither.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base

MEMORY_KINDS = ("FACT", "PREFERENCE", "DECISION", "CONSTRAINT")
MEMORY_STATUSES = ("ACTIVE", "SUPERSEDED", "INVALIDATED")


class WorkspaceMemory(Base):
    __tablename__ = "workspace_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_memories_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_workspace_memories_agent_workspace",
            ondelete="CASCADE",
        ),
        # The thread and the run are provenance, not ownership: a memory
        # survives the deletion of the conversation that taught it, so these
        # null out rather than cascade.
        #
        # The column list after SET NULL is load-bearing. A bare SET NULL nulls
        # every column of the constraint, ``workspace_id`` included, and that
        # column is NOT NULL -- so deleting a thread raised a NotNullViolation
        # instead of forgetting where the memory came from. Only the provenance
        # column may be cleared; the tenant never moves.
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_workspace_memories_thread_workspace",
            ondelete="SET NULL (thread_id)",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_workspace_memories_run_workspace",
            ondelete="SET NULL (source_run_id)",
        ),
        CheckConstraint(
            "kind IN ('FACT', 'PREFERENCE', 'DECISION', 'CONSTRAINT')",
            name="ck_workspace_memories_kind",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUPERSEDED', 'INVALIDATED')",
            name="ck_workspace_memories_status",
        ),
        CheckConstraint("salience > 0", name="ck_workspace_memories_salience_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_workspace_memories_workspace_id"),
        # Deduplication applies to what the agent currently believes, not to
        # what it once believed: a fact that was superseded may legitimately be
        # learned again later, and that has to be allowed to insert.
        Index(
            "uq_workspace_memories_active_hash",
            "workspace_id",
            "agent_id",
            "content_hash",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_workspace_memories_agent", "workspace_id", "agent_id", "status"),
        Index("ix_workspace_memories_run", "workspace_id", "source_run_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    source_run_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="FACT")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    superseded_by_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    # How many separate turns have asserted this. Reinforcement is the only
    # evidence available that a statement is stable rather than incidental, so
    # it is what ranking uses when the budget cannot hold everything.
    salience: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["MEMORY_KINDS", "MEMORY_STATUSES", "WorkspaceMemory"]
