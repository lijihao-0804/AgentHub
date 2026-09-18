"""SQLAlchemy models owned by the M2 model gateway boundary."""

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
    event,
    func,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # ``secret`` remains nullable only for controlled legacy migration. New
    # credentials are written to ``secret_ciphertext`` by the credential service.
    secret: Mapped[str | None] = mapped_column(Text)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    secret_version: Mapped[int | None] = mapped_column(Integer)
    base_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_provider_credentials_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_provider_credentials_workspace_id"),
        Index("ix_provider_credentials_workspace_id", "workspace_id"),
        CheckConstraint(
            "secret IS NOT NULL OR secret_ciphertext IS NOT NULL",
            name="ck_provider_credentials_secret_present",
        ),
    )


@event.listens_for(ProviderCredential, "before_insert")
def _encrypt_provider_credential(_mapper, _connection, target: ProviderCredential) -> None:
    if target.secret is None or target.secret_ciphertext is not None:
        return
    from packages.model_gateway.credentials import (
        CREDENTIAL_CIPHERTEXT_VERSION,
        ProviderCredentialCipher,
    )

    target.secret_ciphertext = ProviderCredentialCipher.from_settings().encrypt(target.secret)
    target.secret_version = CREDENTIAL_CIPHERTEXT_VERSION
    target.secret = None


class ModelProfile(Base):
    __tablename__ = "model_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_model_profiles_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "provider_credential_id"],
            ["provider_credentials.workspace_id", "provider_credentials.id"],
            name="fk_model_profiles_credential_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "fallback_profile_id"],
            ["model_profiles.workspace_id", "model_profiles.id"],
            name="fk_model_profiles_fallback_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint("max_tokens > 0", name="ck_model_profiles_max_tokens_positive"),
        CheckConstraint("timeout_seconds > 0", name="ck_model_profiles_timeout_positive"),
        CheckConstraint("temperature >= 0", name="ck_model_profiles_temperature_nonnegative"),
        UniqueConstraint("workspace_id", "id", name="uq_model_profiles_workspace_id"),
        Index("ix_model_profiles_workspace_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    provider_credential_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    temperature: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), nullable=False, server_default=text("0")
    )
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    fallback_profile_id: Mapped[UUID | None] = mapped_column(SQLUuid(as_uuid=True))
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
