"""M4-A persistence for agent drafts, published versions and bindings."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_agents_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "model_profile_id"],
            ["model_profiles.workspace_id", "model_profiles.id"],
            name="fk_agents_model_profile_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint("prompt_version > 0", name="ck_agents_prompt_version_positive"),
        CheckConstraint(
            "knowledge_binding_mode IN ('PINNED', 'LATEST')",
            name="ck_agents_knowledge_binding_mode",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_agents_workspace_id"),
        Index("ix_agents_workspace_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    model_profile_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    knowledge_binding_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PINNED"
    )
    model_retry_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"max_attempts": 1},
        server_default=text("'{\"max_attempts\": 1}'::jsonb"),
    )
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    runtime_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_versions_agent_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_agent_versions_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_number > 0", name="ck_agent_versions_version_positive"),
        CheckConstraint(
            "spec_schema_version > 0", name="ck_agent_versions_schema_version_positive"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_agent_versions_workspace_id"),
        UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_agent_version"),
        Index("ix_agent_versions_agent_id", "workspace_id", "agent_id", "version_number"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resolved_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)


class AgentKnowledgeBinding(Base):
    __tablename__ = "agent_knowledge_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_knowledge_bindings_agent_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id"],
            ["knowledge_bases.workspace_id", "knowledge_bases.id"],
            name="fk_agent_knowledge_bindings_kb_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "knowledge_base_id", "snapshot_id"],
            [
                "knowledge_snapshots.workspace_id",
                "knowledge_snapshots.knowledge_base_id",
                "knowledge_snapshots.id",
            ],
            name="fk_agent_knowledge_bindings_snapshot_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "binding_mode IN ('PINNED', 'LATEST')", name="ck_agent_knowledge_bindings_mode"
        ),
        CheckConstraint(
            "(binding_mode = 'PINNED' AND snapshot_id IS NOT NULL) OR "
            "(binding_mode = 'LATEST' AND snapshot_id IS NULL)",
            name="ck_agent_knowledge_bindings_snapshot_selector",
        ),
        Index("ix_agent_knowledge_bindings_agent", "workspace_id", "agent_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    binding_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_tools_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_tools_workspace_id"),
        UniqueConstraint("workspace_id", "name", name="uq_tools_workspace_name"),
        Index("ix_tools_workspace_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ToolRevision(Base):
    __tablename__ = "tool_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "tool_id"],
            ["tools.workspace_id", "tools.id"],
            name="fk_tool_revisions_tool_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_tool_revisions_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_number > 0", name="ck_tool_revisions_number_positive"),
        UniqueConstraint("workspace_id", "id", name="uq_tool_revisions_workspace_id"),
        UniqueConstraint("workspace_id", "tool_id", "id", name="uq_tool_revisions_tool_id"),
        UniqueConstraint(
            "workspace_id", "tool_id", "revision_number", name="uq_tool_revisions_number"
        ),
        Index("ix_tool_revisions_tool", "workspace_id", "tool_id", "revision_number"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    tool_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)


class AgentTool(Base):
    __tablename__ = "agent_tools"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agents.workspace_id", "agents.id"],
            name="fk_agent_tools_agent_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_id"],
            ["tools.workspace_id", "tools.id"],
            name="fk_agent_tools_tool_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_id", "tool_revision_id"],
            ["tool_revisions.workspace_id", "tool_revisions.tool_id", "tool_revisions.id"],
            name="fk_agent_tools_revision_workspace",
            ondelete="RESTRICT",
        ),
        Index("ix_agent_tools_agent", "workspace_id", "agent_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    tool_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True)
    tool_revision_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_version_id"],
            ["agent_versions.workspace_id", "agent_versions.id"],
            name="fk_agent_runs_agent_version_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_agent_runs_created_by", ondelete="RESTRICT"
        ),
        # Declared here as well as in 0023 so the metadata a schema-drift check
        # compares against is the schema the migrations actually build. A
        # constraint that exists only in a migration reads to autogenerate as
        # one to drop.
        ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_agent_runs_thread_workspace",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'FAILED', "
            "'NEEDS_ATTENTION', 'CANCEL_REQUESTED', 'CANCELLED')",
            name="ck_agent_runs_status",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_agent_runs_workspace_id"),
        Index("ix_agent_runs_workspace_status", "workspace_id", "status"),
        Index("ix_agent_runs_workspace_created_at", "workspace_id", "created_at"),
        Index("ix_agent_runs_workspace_started_at", "workspace_id", "started_at", "id"),
        Index(
            "ix_agent_runs_workspace_status_started",
            "workspace_id",
            "status",
            "started_at",
        ),
        Index(
            "ix_agent_runs_workspace_version_started",
            "workspace_id",
            "agent_version_id",
            "started_at",
        ),
        Index("ix_agent_runs_reconciliation", "status", "started_at", "id"),
        Index("ix_agent_runs_thread", "workspace_id", "thread_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_version_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    # A back-pointer to the thread this run answered a turn of, and nothing
    # more. Nullable because a Playground run belongs to no thread, and read by
    # nothing that decides how the run executes: replay, evaluation and
    # reconciliation all ignore it.
    thread_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RUNNING")
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    final_output: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(String(96))
    resolved_spec_hash: Mapped[str | None] = mapped_column(String(64))
    effective_knowledge_snapshots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    model_step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_input_tokens: Mapped[int | None] = mapped_column(Integer)
    total_output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    total_cached_tokens: Mapped[int | None] = mapped_column(Integer)
    total_cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    cost_is_estimate: Mapped[bool | None] = mapped_column(Boolean)
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_run_steps_agent_run_workspace",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('PREPARE', 'MODEL', 'TOOL_PROPOSAL', 'POLICY', 'TOOL_EXECUTE', "
            "'OBSERVATION', 'APPROVAL_WAIT', 'ACTION', 'RECONCILIATION', 'GUARD', 'FINISH')",
            name="ck_run_steps_kind",
        ),
        UniqueConstraint(
            "workspace_id", "agent_run_id", "sequence_number", name="uq_run_steps_sequence"
        ),
        Index("ix_run_steps_workspace_run", "workspace_id", "agent_run_id", "sequence_number"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentRunEvent(Base):
    """The durable, ordered log of the events a run published.

    ``run_steps`` is a coarse projection built for humans reading a timeline.
    This table is the stream itself, stored so that a run's life no longer
    depends on the HTTP connection that started it: a consumer that reconnects
    replays from ``sequence`` and misses nothing, and a consumer in a different
    process can follow a run it did not start by tailing this table.

    ``message.delta`` is deliberately **not** persisted.  Keeping it would make
    the log O(tokens) and it buys only the typing animation -- every event that
    carries structure or the final output is kept, so replay reconstructs the
    run, just not the keystrokes.  Sequence gaps are therefore expected and
    ``sequence`` stays a cursor, not a count.
    """

    __tablename__ = "agent_run_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_agent_run_events_agent_run_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "workspace_id", "agent_run_id", "sequence", name="uq_agent_run_events_sequence"
        ),
        Index(
            "ix_agent_run_events_workspace_run",
            "workspace_id",
            "agent_run_id",
            "sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(64))
    # The already-validated safe payload, exactly as it went out over SSE.  The
    # event envelope enforces the field whitelist before it ever reaches here,
    # so nothing provider-specific or secret can land in this column.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "Agent",
    "AgentKnowledgeBinding",
    "AgentTool",
    "AgentVersion",
    "AgentRun",
    "AgentRunEvent",
    "RunStep",
    "Tool",
    "ToolRevision",
]
