"""M4-B read-tool fixture data owned by the tool runtime boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint, func
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_customers_workspace", ondelete="CASCADE"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_customers_workspace_id"),
        UniqueConstraint("workspace_id", "customer_ref", name="uq_customers_workspace_ref"),
        Index("ix_customers_workspace_ref", "workspace_id", "customer_ref"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    customer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_tickets_workspace", ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "customer_id"],
            ["customers.workspace_id", "customers.id"],
            name="fk_tickets_customer_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_tickets_workspace_id"),
        UniqueConstraint("workspace_id", "ticket_ref", name="uq_tickets_workspace_ref"),
        Index("ix_tickets_customer", "workspace_id", "customer_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    ticket_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="OPEN")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["Customer", "Ticket"]
