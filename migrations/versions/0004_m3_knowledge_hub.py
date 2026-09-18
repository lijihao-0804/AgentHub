"""Create M3 Knowledge Hub persistence tables.

Revision ID: 0004_m3_knowledge_hub
Revises: 0003_m2_model_gateway
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_m3_knowledge_hub"
down_revision = "0003_m2_model_gateway"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "knowledge_bases",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_knowledge_bases_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_knowledge_bases_workspace_id"),
    )
    op.create_index(
        "ix_knowledge_bases_workspace_id", "knowledge_bases", ["workspace_id"]
    )

    op.create_table(
        "documents",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("knowledge_base_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            name="fk_documents_knowledge_base_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_documents_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "knowledge_base_id", "id", name="uq_documents_workspace_kb_id"
        ),
    )
    op.create_index(
        "ix_documents_workspace_kb", "documents", ["workspace_id", "knowledge_base_id"]
    )

    op.create_table(
        "document_revisions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("knowledge_base_id", uuid_type, nullable=False),
        sa.Column("document_id", uuid_type, nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("blob_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("media_type", sa.String(length=127), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column(
            "lifecycle_status",
            sa.String(length=16),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "ingestion_status",
            sa.String(length=16),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_id"],
            ["documents.workspace_id", "documents.knowledge_base_id", "documents.id"],
            name="fk_document_revisions_document_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_document_revisions_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "knowledge_base_id",
            "id",
            name="uq_document_revisions_workspace_kb_id",
        ),
        sa.UniqueConstraint("document_id", "revision_number", name="uq_document_revisions_number"),
        sa.CheckConstraint(
            "lifecycle_status IN ('ACTIVE', 'RETIRED', 'DELETED')",
            name="ck_document_revisions_lifecycle_status",
        ),
        sa.CheckConstraint(
            "ingestion_status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_document_revisions_ingestion_status",
        ),
    )
    op.create_index(
        "ix_document_revisions_workspace_kb",
        "document_revisions",
        ["workspace_id", "knowledge_base_id"],
    )
    op.create_index(
        "ix_document_revisions_document_status",
        "document_revisions",
        ["document_id", "ingestion_status"],
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("chunk_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("knowledge_base_id", uuid_type, nullable=False),
        sa.Column("document_id", uuid_type, nullable=False),
        sa.Column("document_revision_id", uuid_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("normalized_content_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "locator", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_id"],
            ["documents.workspace_id", "documents.knowledge_base_id", "documents.id"],
            name="fk_document_chunks_document_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            name="fk_document_chunks_revision_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("chunk_id", name="uq_document_chunks_chunk_id"),
    )
    op.create_index(
        "ix_document_chunks_revision",
        "document_chunks",
        ["workspace_id", "document_revision_id"],
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("knowledge_base_id", uuid_type, nullable=False),
        sa.Column("document_revision_id", uuid_type, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("stage", sa.String(length=16), nullable=False, server_default="PARSING"),
        sa.Column("lease_token", sa.String(length=64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=64)),
        sa.Column("safe_error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            name="fk_ingestion_jobs_revision_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_ingestion_jobs_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id", "document_revision_id", name="uq_ingestion_jobs_revision"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ck_ingestion_jobs_status",
        ),
        sa.CheckConstraint(
            "stage IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')",
            name="ck_ingestion_jobs_stage",
        ),
    )
    op.create_index(
        "ix_ingestion_jobs_reconciliation",
        "ingestion_jobs",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "knowledge_snapshots",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("knowledge_base_id", uuid_type, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            name="fk_knowledge_snapshots_knowledge_base_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_knowledge_snapshots_workspace_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "knowledge_base_id",
            "content_hash",
            name="uq_knowledge_snapshots_content_hash",
        ),
    )
    op.create_index(
        "ix_knowledge_snapshots_workspace_kb",
        "knowledge_snapshots",
        ["workspace_id", "knowledge_base_id"],
    )

    op.create_table(
        "knowledge_snapshot_items",
        sa.Column("workspace_id", uuid_type, primary_key=True),
        sa.Column("snapshot_id", uuid_type, primary_key=True),
        sa.Column("knowledge_base_id", uuid_type, primary_key=True),
        sa.Column("document_id", uuid_type, primary_key=True),
        sa.Column("document_revision_id", uuid_type, primary_key=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["knowledge_snapshots.workspace_id", "knowledge_snapshots.id"],
            name="fk_snapshot_items_snapshot_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_id"],
            ["documents.workspace_id", "documents.knowledge_base_id", "documents.id"],
            name="fk_snapshot_items_document_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "document_revision_id"],
            [
                "document_revisions.workspace_id",
                "document_revisions.knowledge_base_id",
                "document_revisions.id",
            ],
            name="fk_snapshot_items_revision_workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "snapshot_id",
            "document_revision_id",
            name="uq_snapshot_items_revision",
        ),
    )
    op.create_index(
        "ix_snapshot_items_snapshot",
        "knowledge_snapshot_items",
        ["workspace_id", "snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_snapshot_items_snapshot", table_name="knowledge_snapshot_items")
    op.drop_table("knowledge_snapshot_items")
    op.drop_index("ix_knowledge_snapshots_workspace_kb", table_name="knowledge_snapshots")
    op.drop_table("knowledge_snapshots")
    op.drop_index("ix_ingestion_jobs_reconciliation", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_document_chunks_revision", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_document_revisions_document_status", table_name="document_revisions")
    op.drop_index("ix_document_revisions_workspace_kb", table_name="document_revisions")
    op.drop_table("document_revisions")
    op.drop_index("ix_documents_workspace_kb", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_knowledge_bases_workspace_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
