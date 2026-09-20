"""Persistence for threads and their turns.

A thread owns no execution semantics. It holds no checkpoint, no approval, no
model binding and no tool binding; it only records that a sequence of runs
belongs to one piece of a user's work. Everything that decides how a run
behaves still lives on the AgentVersion the run was bound to.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class AgentThread(Base):
    """One continuous piece of work against one agent.

    The binding is to ``agent_id`` and not to a version on purpose. Each turn
    resolves the agent's current published version at submission time, so a
    long thread may span versions. The reproducible unit is the run, not the
    thread, and pinning a thread to a version would quietly make the thread the
    reproducible unit instead.
    """

    __tablename__ = "agent_threads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_threads_agent_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_agent_threads_created_by", ondelete="RESTRICT"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_agent_threads_workspace_id"),
        Index("ix_agent_threads_workspace_updated_at", "workspace_id", "updated_at", "id"),
        Index("ix_agent_threads_workspace_agent", "workspace_id", "agent_id", "updated_at"),
        Index("ix_agent_threads_workspace_kind", "workspace_id", "kind", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Which application surface reads this thread. A routing label only: no
    # runtime behaviour branches on it. See packages.threads.kinds.
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'general'"), default="general"
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadTurn(Base):
    """One question the user asked, and the run that answered it."""

    __tablename__ = "thread_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_thread_turns_thread_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_thread_turns_run_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_thread_turns_workspace_id"),
        UniqueConstraint("thread_id", "sequence", name="uq_thread_turns_sequence"),
        UniqueConstraint("thread_id", "client_token", name="uq_thread_turns_client_token"),
        Index("ix_thread_turns_thread_sequence", "workspace_id", "thread_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    agent_run_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    client_token: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["AgentThread", "ThreadTurn"]
