"""Add interactive dataset category.

Revision ID: 0050_interactive_category
Revises: 0049_evaluation_evidence_level
"""

from __future__ import annotations

from alembic import op

revision = "0050_interactive_category"
down_revision = "0049_evaluation_evidence_level"
branch_labels = None
depends_on = None

_OLD = (
    "category IN ('RETRIEVAL', 'KNOWLEDGE_QA', 'TOOL', 'NO_ANSWER', "
    "'APPROVAL', 'MULTI_STEP', 'FAILURE')"
)
_NEW = (
    "category IN ('RETRIEVAL', 'KNOWLEDGE_QA', 'TOOL', 'NO_ANSWER', "
    "'APPROVAL', 'MULTI_STEP', 'FAILURE', 'INTERACTIVE')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_evaluation_dataset_items_category",
        "evaluation_dataset_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_evaluation_dataset_items_category", "evaluation_dataset_items", _NEW
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_evaluation_dataset_items_category",
        "evaluation_dataset_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_evaluation_dataset_items_category", "evaluation_dataset_items", _OLD
    )
