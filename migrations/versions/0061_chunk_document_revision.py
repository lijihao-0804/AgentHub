"""Require a stored chunk's revision to belong to its recorded document."""

import sqlalchemy as sa
from alembic import op

revision = "0061_chunk_document_revision"
down_revision = "0060_private_memory"
branch_labels = None
depends_on = None

OLD = "fk_document_chunks_revision_workspace"
NEW = "fk_document_chunks_document_revision"


def upgrade() -> None:
    # NOT VALID checks new writes immediately. Existing rows are diagnosed
    # and validated in the same transaction, preserving the old FK on failure.
    op.create_foreign_key(
        NEW,
        "document_chunks",
        "document_revisions",
        ["workspace_id", "knowledge_base_id", "document_id", "document_revision_id"],
        ["workspace_id", "knowledge_base_id", "document_id", "id"],
        ondelete="CASCADE",
        postgresql_not_valid=True,
    )
    example = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT c.id, c.document_id, c.document_revision_id, r.document_id AS revision_owner
        FROM document_chunks c JOIN document_revisions r ON r.id = c.document_revision_id
        WHERE c.document_id <> r.document_id ORDER BY c.id LIMIT 1
    """)
        )
        .mappings()
        .first()
    )
    if example:
        raise RuntimeError(
            f"Document/revision mismatch for document_chunks.id={example['id']}: "
            f"document_id={example['document_id']}, revision_id={example['document_revision_id']}, "
            f"revision owner={example['revision_owner']}. Review provenance before upgrading."
        )
    op.execute(f"ALTER TABLE document_chunks VALIDATE CONSTRAINT {NEW}")
    op.drop_constraint(OLD, "document_chunks", type_="foreignkey")


def downgrade() -> None:
    op.create_foreign_key(
        OLD,
        "document_chunks",
        "document_revisions",
        ["workspace_id", "knowledge_base_id", "document_revision_id"],
        ["workspace_id", "knowledge_base_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(NEW, "document_chunks", type_="foreignkey")
