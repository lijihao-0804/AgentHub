"""Persistence for artifacts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
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


class Artifact(Base):
    """A structured work product belonging to a thread.

    ``run_id`` being set is what makes an artifact a record rather than a
    document: it was produced by one execution and is not editable afterwards.
    A user who wants to change it saves their own copy, which has no run.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_artifacts_thread_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_artifacts_run_workspace",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_artifacts_created_by", ondelete="RESTRICT"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_artifacts_workspace_id"),
        Index("ix_artifacts_thread_created_at", "workspace_id", "thread_id", "created_at", "id"),
        Index("ix_artifacts_thread_type", "workspace_id", "thread_id", "type"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["Artifact"]
