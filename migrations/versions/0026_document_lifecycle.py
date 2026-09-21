"""Model the kind of retirement that spans two documents.

``document_revisions.lifecycle_status`` already retires *a revision of one
document*: upload a new revision and the previous one stops being snapshotted.
It cannot express the other, more common kind of retirement, where a policy is
replaced by a **separate document** -- ``password-policy-v1`` superseded by
``password-policy-v2``. Those are two ``documents`` rows and nothing linked
them, so ranking saw two equally authoritative files that say nearly the same
thing in nearly the same words, and could not prefer the live one.

Measured on a 60-document corpus before this migration: of 14 questions whose
answer depends on picking the current version, 4 ranked a retired document
above the successor that replaced it. That is not a tuning problem -- there was
no signal to tune against.

``effective_date`` is deliberately a plain date, not a range. "When did this
take effect" is what documents actually state; a validity interval would invite
a temporal query engine this system has no use for.

The self reference is single-column, unlike the composite foreign keys used
everywhere else in the knowledge schema, because ``ON DELETE SET NULL`` on a
composite key would also null ``workspace_id``, which is NOT NULL. The
same-knowledge-base rule is therefore enforced in the service layer.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0026_document_lifecycle"
down_revision: str | None = "0025_thread_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("effective_date", sa.Date(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("superseded_by_document_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_documents_superseded_by",
        "documents",
        "documents",
        ["superseded_by_document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_documents_supersession_not_self",
        "documents",
        "superseded_by_document_id IS NULL OR superseded_by_document_id <> id",
    )
    # Retrieval asks "is this document superseded" for every chunk it is about
    # to return, and the answer is NULL for almost every row. A partial index
    # keeps that lookup off the table without paying for the common case.
    op.create_index(
        "ix_documents_superseded_by",
        "documents",
        ["superseded_by_document_id"],
        postgresql_where=sa.text("superseded_by_document_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_superseded_by", table_name="documents")
    op.drop_constraint("ck_documents_supersession_not_self", "documents", type_="check")
    op.drop_constraint("fk_documents_superseded_by", "documents", type_="foreignkey")
    op.drop_column("documents", "superseded_by_document_id")
    op.drop_column("documents", "effective_date")
