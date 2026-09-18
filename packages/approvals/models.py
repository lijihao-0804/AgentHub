"""SQLAlchemy persistence for the M5 Approval domain."""

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
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_approvals_run_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name="fk_approvals_agent_version_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_revision_id"],
            ["tool_revisions.workspace_id", "tool_revisions.id"],
            name="fk_approvals_tool_revision_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name="fk_approvals_requested_by", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name="fk_approvals_decided_by", ondelete="RESTRICT"
        ),
        CheckConstraint(
            "decision_status IN ('PENDING', 'APPROVED', 'DENIED', 'EXPIRED', 'CANCELLED')",
            name="ck_approvals_decision_status",
        ),
        CheckConstraint(
            "execution_status IN ('NOT_STARTED', 'CLAIMED', 'SUCCEEDED', 'FAILED', "
            "'UNKNOWN_OUTCOME')",
            name="ck_approvals_execution_status",
        ),
        UniqueConstraint(
            "workspace_id", "logical_action_id", name="uq_approvals_workspace_logical_action"
        ),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_approvals_workspace_idempotency"
        ),
        Index("ix_approvals_workspace_decision", "workspace_id", "decision_status"),
        Index("ix_approvals_workspace_run", "workspace_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    logical_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_revision_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    tool_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PENDING"
    )
    execution_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="NOT_STARTED"
    )
    requested_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    decided_by: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(96))
    safe_failure_message: Mapped[str | None] = mapped_column(Text)
    safe_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    execution_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["Approval"]
