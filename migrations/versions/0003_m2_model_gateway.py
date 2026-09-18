"""Create M2 provider credential and model profile tables.

Revision ID: 0003_m2_model_gateway
Revises: 0002_m1_auth_tenant_rbac
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_m2_model_gateway"
down_revision = "0002_m1_auth_tenant_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "provider_credentials",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_provider_credentials_workspace", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_provider_credentials_workspace_id"),
    )
    op.create_index(
        "ix_provider_credentials_workspace_id", "provider_credentials", ["workspace_id"]
    )

    op.create_table(
        "model_profiles",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("provider_credential_id", uuid_type, nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "temperature",
            sa.Numeric(precision=6, scale=3),
            nullable=False,
            server_default="0",
        ),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column("fallback_profile_id", uuid_type),
        sa.Column(
            "capabilities",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint(
            "max_tokens > 0", name="ck_model_profiles_max_tokens_positive"
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0", name="ck_model_profiles_timeout_positive"
        ),
        sa.CheckConstraint(
            "temperature >= 0", name="ck_model_profiles_temperature_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"],
            name="fk_model_profiles_workspace", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "provider_credential_id"],
            ["provider_credentials.workspace_id", "provider_credentials.id"],
            name="fk_model_profiles_credential_workspace", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "fallback_profile_id"],
            ["model_profiles.workspace_id", "model_profiles.id"],
            name="fk_model_profiles_fallback_workspace", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_model_profiles_workspace_id"),
    )
    op.create_index("ix_model_profiles_workspace_id", "model_profiles", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_model_profiles_workspace_id", table_name="model_profiles")
    op.drop_table("model_profiles")
    op.drop_index("ix_provider_credentials_workspace_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
