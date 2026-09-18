"""Knowledge Hub persistence models and state enums."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
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
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class RevisionLifecycleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    DELETED = "DELETED"


class RevisionIngestionStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class IngestionJobStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class IngestionStage(StrEnum):
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_knowledge_bases_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_knowledge_bases_workspace_id"),
        Index("ix_knowledge_bases_workspace_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            name="fk_documents_knowledge_base_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_documents_workspace_id"),
        UniqueConstraint(
            "workspace_id", "knowledge_base_id", "id", name="uq_documents_workspace_kb_id"
        ),
        Index("ix_documents_workspace_kb", "workspace_id", "knowledge_base_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentRevision(Base):
    __tablename__ = "document_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_id"],
            ["documents.workspace_id", "documents.knowledge_base_id", "documents.id"],
            name="fk_document_revisions_document_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_document_revisions_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "knowledge_base_id",
            "id",
            name="uq_document_revisions_workspace_kb_id",
        ),
        UniqueConstraint("document_id", "revision_number", name="uq_document_revisions_number"),
        CheckConstraint(
            "lifecycle_status IN ('ACTIVE', 'RETIRED', 'DELETED')",
            name="ck_document_revisions_lifecycle_status",
        ),
        CheckConstraint(
            "ingestion_status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_document_revisions_ingestion_status",
        ),
        Index("ix_document_revisions_workspace_kb", "workspace_id", "knowledge_base_id"),
        Index("ix_document_revisions_document_status", "document_id", "ingestion_status"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    blob_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=RevisionLifecycleStatus.ACTIVE
    )
    ingestion_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=RevisionIngestionStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_id"],
            ["documents.workspace_id", "documents.knowledge_base_id", "documents.id"],
            name="fk_document_chunks_document_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            name="fk_document_chunks_revision_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("chunk_id", name="uq_document_chunks_chunk_id"),
        Index("ix_document_chunks_revision", "workspace_id", "document_revision_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    document_revision_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            name="fk_ingestion_jobs_revision_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_ingestion_jobs_workspace_id"),
        UniqueConstraint(
            "workspace_id", "document_revision_id", name="uq_ingestion_jobs_revision"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ck_ingestion_jobs_status",
        ),
        CheckConstraint(
            "stage IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')",
            name="ck_ingestion_jobs_stage",
        ),
        Index("ix_ingestion_jobs_reconciliation", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    document_revision_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IngestionJobStatus.PENDING
    )
    stage: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IngestionStage.PARSING
    )
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    safe_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeSnapshot(Base):
    __tablename__ = "knowledge_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            name="fk_knowledge_snapshots_knowledge_base_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_knowledge_snapshots_workspace_id"),
        UniqueConstraint(
            "workspace_id",
            "knowledge_base_id",
            "content_hash",
            name="uq_knowledge_snapshots_content_hash",
        ),
        Index("ix_knowledge_snapshots_workspace_kb", "workspace_id", "knowledge_base_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeSnapshotItem(Base):
    __tablename__ = "knowledge_snapshot_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["knowledge_snapshots.workspace_id", "knowledge_snapshots.id"],
            name="fk_snapshot_items_snapshot_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_id"],
            ["documents.workspace_id", "documents.knowledge_base_id", "documents.id"],
            name="fk_snapshot_items_document_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            name="fk_snapshot_items_revision_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "snapshot_id",
            "document_revision_id",
            name="uq_snapshot_items_revision",
        ),
        Index("ix_snapshot_items_snapshot", "workspace_id", "snapshot_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    document_revision_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
