"""Wiki revocation tombstones survive raw ingestion lifecycle reconciliation."""
import sqlalchemy as sa
from alembic import op

revision = "0033_wiki_source_revocation"
down_revision = "0032_wiki"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("wiki_source_revocations",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("knowledge_base_id", sa.Uuid(), primary_key=True),
        sa.Column("document_revision_id", sa.Uuid(), primary_key=True),
        sa.Column("revoked_by", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id", "knowledge_base_id", "document_revision_id"],
            ["document_revisions.workspace_id", "document_revisions.knowledge_base_id",
             "document_revisions.id"], ondelete="RESTRICT"))
    op.execute("CREATE TRIGGER wiki_revocation_guard BEFORE UPDATE OR DELETE ON "
               "wiki_source_revocations FOR EACH ROW EXECUTE FUNCTION wiki_guard_extraction()")


def downgrade():
    op.drop_table("wiki_source_revocations")
