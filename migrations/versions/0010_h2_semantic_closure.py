"""Add H2 immutable run projections and encrypted credential storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_h2_semantic_closure"
down_revision: str | None = "0009_m4c_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("resolved_spec_hash", sa.String(length=64)))
    op.add_column(
        "agent_runs",
        sa.Column(
            "effective_knowledge_snapshots",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("agent_runs", sa.Column("total_input_tokens", sa.Integer()))
    op.add_column("agent_runs", sa.Column("total_output_tokens", sa.Integer()))
    op.add_column("agent_runs", sa.Column("total_tokens", sa.Integer()))
    op.add_column("agent_runs", sa.Column("total_cached_tokens", sa.Integer()))
    op.add_column("agent_runs", sa.Column("total_cost_amount", sa.Numeric(20, 8)))
    op.add_column("agent_runs", sa.Column("cost_currency", sa.String(length=3)))
    op.add_column("agent_runs", sa.Column("cost_is_estimate", sa.Boolean()))

    op.alter_column("provider_credentials", "secret", nullable=True)
    op.add_column("provider_credentials", sa.Column("secret_ciphertext", sa.Text()))
    op.add_column("provider_credentials", sa.Column("secret_version", sa.Integer()))
    op.create_check_constraint(
        "ck_provider_credentials_secret_present",
        "provider_credentials",
        "secret IS NOT NULL OR secret_ciphertext IS NOT NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    encrypted_only = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM provider_credentials
            WHERE secret IS NULL AND secret_ciphertext IS NOT NULL
            LIMIT 1
            """
        )
    ).first()
    if encrypted_only is not None:
        raise RuntimeError(
            "0010 downgrade is blocked while encrypted-only provider credentials exist; "
            "decrypt and rewrite those rows as legacy plaintext before retrying."
        )
    op.drop_constraint(
        "ck_provider_credentials_secret_present", "provider_credentials", type_="check"
    )
    op.drop_column("provider_credentials", "secret_version")
    op.drop_column("provider_credentials", "secret_ciphertext")
    op.alter_column("provider_credentials", "secret", nullable=False)

    op.drop_column("agent_runs", "cost_is_estimate")
    op.drop_column("agent_runs", "cost_currency")
    op.drop_column("agent_runs", "total_cost_amount")
    op.drop_column("agent_runs", "total_cached_tokens")
    op.drop_column("agent_runs", "total_tokens")
    op.drop_column("agent_runs", "total_output_tokens")
    op.drop_column("agent_runs", "total_input_tokens")
    op.drop_column("agent_runs", "effective_knowledge_snapshots")
    op.drop_column("agent_runs", "resolved_spec_hash")
