"""Operational handoff state; source artifacts remain immutable evidence."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class HandoffCase(Base):
    __tablename__ = "handoff_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_artifact_id"],
            ["artifacts.workspace_id", "artifacts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("source_artifact_id", name="uq_handoff_source"),
        CheckConstraint(
            "status IN ('OPEN', 'ASSIGNED', 'IN_PROGRESS', 'CLOSED')", name="ck_handoff_status"
        ),
        CheckConstraint("version >= 1", name="ck_handoff_version"),
        CheckConstraint(
            "(status = 'OPEN' AND assignee_id IS NULL "
            "AND claimed_by IS NULL AND closed_by IS NULL) OR "
            "(status = 'ASSIGNED' AND assignee_id IS NOT NULL "
            "AND claimed_by IS NULL AND closed_by IS NULL) OR "
            "(status = 'IN_PROGRESS' AND assignee_id IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claimed_by = assignee_id AND closed_by IS NULL) OR "
            "(status = 'CLOSED' AND assignee_id IS NOT NULL AND claimed_by IS NOT NULL "
            "AND closed_by IS NOT NULL AND claimed_by = assignee_id AND closed_by = assignee_id "
            "AND closure_reason IS NOT NULL AND close_request_hash IS NOT NULL)",
            name="ck_handoff_state_fields",
        ),
        Index("ix_handoff_workspace_thread", "workspace_id", "thread_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    source_artifact_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(
        SQLUuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[UUID | None] = mapped_column(
        SQLUuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    claimed_by: Mapped[UUID | None] = mapped_column(
        SQLUuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    closed_by: Mapped[UUID | None] = mapped_column(
        SQLUuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    closure_reason: Mapped[str | None] = mapped_column(Text)
    unresolved_items: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    close_request_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
