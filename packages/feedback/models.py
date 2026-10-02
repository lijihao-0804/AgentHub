from __future__ import annotations

from datetime import datetime
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
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class RunFeedback(Base):
    __tablename__ = "run_feedback"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "imported_version_id"],
            ["evaluation_dataset_versions.workspace_id", "evaluation_dataset_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="RESTRICT"),
        UniqueConstraint("workspace_id", "created_by", "client_key", name="uq_feedback_submission"),
        CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
        CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')", name="ck_feedback_status"),
        CheckConstraint(
            "category IN ('FACTUAL', 'RETRIEVAL', 'TOOL', 'LATENCY', 'EXPRESSION', 'OTHER')",
            name="ck_feedback_category",
        ),
        Index("ix_feedback_workspace_run", "workspace_id", "run_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    # Historical turn identity is retained if a conversation is deleted; authorization
    # and the run/turn relation are checked at submission, not inferred at read time.
    turn_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    agent_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    client_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    content_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    corrected_answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    imported_version_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    import_request_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
