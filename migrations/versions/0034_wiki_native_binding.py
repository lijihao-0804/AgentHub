"""Wiki native binding: draft binding table plus frozen publication columns."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_wiki_native_binding"
down_revision = "0033_wiki_source_revocation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_wiki_bindings",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), primary_key=True),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("binding_mode", sa.String(16), nullable=False),
        sa.Column("wiki_publication_id", sa.Uuid()),
        sa.Column("publication_hash", sa.String(64)),
        sa.CheckConstraint(
            "binding_mode IN ('PINNED', 'LATEST')", name="ck_agent_wiki_bindings_mode"
        ),
        sa.CheckConstraint(
            "(binding_mode = 'PINNED' AND wiki_publication_id IS NOT NULL "
            "AND publication_hash IS NOT NULL) OR "
            "(binding_mode = 'LATEST' AND wiki_publication_id IS NULL "
            "AND publication_hash IS NULL)",
            name="ck_agent_wiki_bindings_selector",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"], ["agents.workspace_id", "agents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "wiki_publication_id"],
            [
                "wiki_compilations.workspace_id",
                "wiki_compilations.knowledge_base_id",
                "wiki_compilations.id",
            ],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_agent_wiki_bindings_agent", "agent_wiki_bindings", ["workspace_id", "agent_id"]
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "effective_wiki_publications",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "evaluation_experiment_variants",
        sa.Column(
            "effective_wiki_publications",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("evaluation_experiment_variants", "effective_wiki_publications")
    op.drop_column("agent_runs", "effective_wiki_publications")
    op.drop_index("ix_agent_wiki_bindings_agent", table_name="agent_wiki_bindings")
    op.drop_table("agent_wiki_bindings")
