"""Create M4-A agent draft and immutable publish persistence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_m4a_agent_publish"
down_revision: str | None = "0006_m3f_snapshot_doc_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "agents",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("model_profile_id", uuid_type, nullable=False),
        sa.Column(
            "knowledge_binding_mode",
            sa.String(length=16),
            nullable=False,
            server_default="PINNED",
        ),
        sa.Column(
            "model_retry_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{\"max_attempts\": 1}'::jsonb"),
        ),
        sa.Column(
            "retrieval_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "runtime_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.CheckConstraint("prompt_version > 0", name="ck_agents_prompt_version_positive"),
        sa.CheckConstraint(
            "knowledge_binding_mode IN ('PINNED', 'LATEST')",
            name="ck_agents_knowledge_binding_mode",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_agents_workspace", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "model_profile_id"],
            ["model_profiles.workspace_id", "model_profiles.id"],
            name="fk_agents_model_profile_workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_agents_workspace_id"),
    )
    op.create_index("ix_agents_workspace_id", "agents", ["workspace_id"])

    op.create_table(
        "tools",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_tools_workspace", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tools_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_tools_workspace_name"),
    )
    op.create_index("ix_tools_workspace_id", "tools", ["workspace_id"])

    op.create_table(
        "tool_revisions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("tool_id", uuid_type, nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.CheckConstraint(
            "revision_number > 0", name="ck_tool_revisions_number_positive"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "tool_id"],
            ["tools.workspace_id", "tools.id"],
            name="fk_tool_revisions_tool_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_tool_revisions_created_by", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tool_revisions_workspace_id"),
        sa.UniqueConstraint("workspace_id", "tool_id", "id", name="uq_tool_revisions_tool_id"),
        sa.UniqueConstraint(
            "workspace_id", "tool_id", "revision_number", name="uq_tool_revisions_number"
        ),
    )
    op.create_index(
        "ix_tool_revisions_tool", "tool_revisions", ["workspace_id", "tool_id", "revision_number"]
    )

    op.create_table(
        "agent_versions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("agent_id", uuid_type, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("spec_schema_version", sa.Integer(), nullable=False),
        sa.Column("resolved_spec", postgresql.JSONB(), nullable=False),
        sa.Column("resolved_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.CheckConstraint(
            "version_number > 0", name="ck_agent_versions_version_positive"
        ),
        sa.CheckConstraint(
            "spec_schema_version > 0", name="ck_agent_versions_schema_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_versions_agent_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_agent_versions_created_by", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_agent_versions_workspace_id"),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_agent_version"),
    )
    op.create_index(
        "ix_agent_versions_agent_id",
        "agent_versions",
        ["workspace_id", "agent_id", "version_number"],
    )

    op.create_table(
        "agent_knowledge_bindings",
        sa.Column("workspace_id", uuid_type, primary_key=True),
        sa.Column("agent_id", uuid_type, primary_key=True),
        sa.Column("knowledge_base_id", uuid_type, primary_key=True),
        sa.Column("binding_mode", sa.String(length=16), nullable=False),
        sa.Column("snapshot_id", uuid_type),
        sa.CheckConstraint(
            "binding_mode IN ('PINNED', 'LATEST')", name="ck_agent_knowledge_bindings_mode"
        ),
        sa.CheckConstraint(
            "(binding_mode = 'PINNED' AND snapshot_id IS NOT NULL) OR "
            "(binding_mode = 'LATEST' AND snapshot_id IS NULL)",
            name="ck_agent_knowledge_bindings_snapshot_selector",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_knowledge_bindings_agent_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            name="fk_agent_knowledge_bindings_kb_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "snapshot_id"],
            [
                "knowledge_snapshots.workspace_id",
                "knowledge_snapshots.knowledge_base_id",
                "knowledge_snapshots.id",
            ],
            name="fk_agent_knowledge_bindings_snapshot_workspace",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_agent_knowledge_bindings_agent",
        "agent_knowledge_bindings",
        ["workspace_id", "agent_id"],
    )

    op.create_table(
        "agent_tools",
        sa.Column("workspace_id", uuid_type, primary_key=True),
        sa.Column("agent_id", uuid_type, primary_key=True),
        sa.Column("tool_id", uuid_type, primary_key=True),
        sa.Column("tool_revision_id", uuid_type),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_tools_agent_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "tool_id"],
            ["tools.workspace_id", "tools.id"],
            name="fk_agent_tools_tool_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "tool_id", "tool_revision_id"],
            ["tool_revisions.workspace_id", "tool_revisions.tool_id", "tool_revisions.id"],
            name="fk_agent_tools_revision_workspace",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_agent_tools_agent", "agent_tools", ["workspace_id", "agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_tools_agent", table_name="agent_tools")
    op.drop_table("agent_tools")
    op.drop_index("ix_agent_knowledge_bindings_agent", table_name="agent_knowledge_bindings")
    op.drop_table("agent_knowledge_bindings")
    op.drop_index("ix_agent_versions_agent_id", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("ix_tool_revisions_tool", table_name="tool_revisions")
    op.drop_table("tool_revisions")
    op.drop_index("ix_tools_workspace_id", table_name="tools")
    op.drop_table("tools")
    op.drop_index("ix_agents_workspace_id", table_name="agents")
    op.drop_table("agents")
