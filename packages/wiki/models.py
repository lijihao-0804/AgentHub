"""Wiki publication, durable compilation and immutable revision extraction cache."""

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
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class WikiCompilation(Base):
    __tablename__ = "wiki_compilations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "source_snapshot_id"],
            [
                "knowledge_snapshots.workspace_id",
                "knowledge_snapshots.knowledge_base_id",
                "knowledge_snapshots.id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "knowledge_base_id", "id"),
        UniqueConstraint("workspace_id", "knowledge_base_id", "identity_hash"),
        CheckConstraint("status IN ('PENDING','PROCESSING','READY','PUBLISHED','FAILED')"),
        Index("ix_wiki_jobs_reconcile", "status", "lease_expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_snapshot_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    telemetry: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(64))


class WikiExtraction(Base):
    __tablename__ = "wiki_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "document_revision_id", "compiler_hash"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    document_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    compiler_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    telemetry: Mapped[dict] = mapped_column(JSONB, nullable=False)


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "compilation_id"],
            [
                "wiki_compilations.workspace_id",
                "wiki_compilations.knowledge_base_id",
                "wiki_compilations.id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("compilation_id", "page_key"),
        CheckConstraint("kind IN ('concept','entity','topic')"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    compilation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    page_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    claims: Mapped[list] = mapped_column(JSONB, nullable=False)
    relations: Mapped[list] = mapped_column(JSONB, nullable=False)


class WikiSourceRevocation(Base):
    """Persistent denial survives legacy ingestion recalculating raw lifecycle flags."""

    __tablename__ = "wiki_source_revocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    document_revision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    revoked_by: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
