"""Store the KB default and freeze the effective chunking policy per new revision."""

import sqlalchemy as sa
from alembic import op

revision = "0058_revision_chunking"
down_revision = "0057_delegated_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column("chunking_strategy", sa.String(20), nullable=False, server_default="fixed_v1"),
    )
    op.create_check_constraint(
        "ck_knowledge_bases_chunking_strategy",
        "knowledge_bases",
        "chunking_strategy IN ('fixed_v1', 'structural_v1', 'adaptive_v1')",
    )
    op.add_column("document_revisions", sa.Column("chunking_strategy", sa.String(20)))
    op.add_column("document_revisions", sa.Column("chunk_size_chars", sa.Integer()))
    op.add_column("document_revisions", sa.Column("chunk_overlap_chars", sa.Integer()))
    op.create_check_constraint(
        "ck_document_revisions_chunking_policy",
        "document_revisions",
        "(chunking_strategy IS NULL AND chunk_size_chars IS NULL AND chunk_overlap_chars IS NULL)"
        " OR (chunking_strategy IS NOT NULL"
        " AND chunking_strategy IN ('fixed_v1', 'structural_v1', 'adaptive_v1')"
        " AND chunk_size_chars IS NOT NULL AND chunk_size_chars > 0"
        " AND chunk_overlap_chars IS NOT NULL AND chunk_overlap_chars >= 0"
        " AND chunk_overlap_chars < chunk_size_chars"
        " AND (chunking_strategy = 'fixed_v1' OR chunk_overlap_chars = 0))",
    )


def downgrade() -> None:
    op.execute("LOCK TABLE knowledge_bases, document_revisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM knowledge_bases WHERE chunking_strategy <> 'fixed_v1')
               OR EXISTS (SELECT 1 FROM document_revisions WHERE chunking_strategy IS NOT NULL)
            THEN
                RAISE EXCEPTION '0058 downgrade refused: retained chunking policies would be lost';
            END IF;
        END $$;
    """)
    op.drop_constraint("ck_document_revisions_chunking_policy", "document_revisions", type_="check")
    op.drop_column("document_revisions", "chunk_overlap_chars")
    op.drop_column("document_revisions", "chunk_size_chars")
    op.drop_column("document_revisions", "chunking_strategy")
    op.drop_constraint("ck_knowledge_bases_chunking_strategy", "knowledge_bases", type_="check")
    op.drop_column("knowledge_bases", "chunking_strategy")
