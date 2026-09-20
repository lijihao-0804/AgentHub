"""Persistence for workspace-managed remote MCP connections."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class McpAuthType(StrEnum):
    NONE = "NONE"
    BEARER = "BEARER"


class McpConnection(Base):
    """A remote MCP server this workspace is configured to talk to.

    There is no plaintext secret column. Unlike the older provider credentials
    table, which carries one for a controlled legacy migration, this table was
    born encrypted: the only place a bearer token can be written is
    ``secret_ciphertext``, so "never stored in plaintext" is a property of the
    schema rather than a rule the service has to keep remembering.
    """

    __tablename__ = "mcp_connections"

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    secret_version: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_mcp_connections_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_mcp_connections_created_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_mcp_connections_workspace_id"),
        UniqueConstraint("workspace_id", "name", name="uq_mcp_connections_workspace_name"),
        Index("ix_mcp_connections_workspace_id", "workspace_id"),
        CheckConstraint(
            "auth_type IN ('NONE', 'BEARER')",
            name="ck_mcp_connections_auth_type",
        ),
        # BEARER always carries a token; NONE never does. Stating the biconditional
        # in the database keeps a half-applied write from producing a connection
        # that claims one auth model and stores another.
        CheckConstraint(
            "(auth_type = 'BEARER') = (secret_ciphertext IS NOT NULL)",
            name="ck_mcp_connections_secret_matches_auth_type",
        ),
    )


__all__ = ["McpAuthType", "McpConnection"]
