"""Workspace-scoped persistence for model credentials and profiles."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.config.settings import Settings, get_settings
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.credentials import CredentialEncryptionError, ProviderCredentialCipher
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile, ProviderCredential


def _workspace_id(context: WorkspaceExecutionContext) -> UUID | None:
    try:
        return UUID(context.workspace_id)
    except ValueError:
        return None


class SqlAlchemyModelGatewayRepository:
    """Repository methods intentionally require execution context for every lookup."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        credential_cipher: ProviderCredentialCipher | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.credential_cipher = credential_cipher
        self.settings = settings

    async def get_provider_credential(
        self,
        context: WorkspaceExecutionContext,
        credential_id: UUID,
    ) -> ProviderCredential | None:
        workspace_id = _workspace_id(context)
        if workspace_id is None:
            return None
        result = await self.session.execute(
            select(ProviderCredential).where(
                ProviderCredential.id == credential_id,
                ProviderCredential.workspace_id == workspace_id,
            )
        )
        credential = result.scalar_one_or_none()
        if credential is not None and credential.secret_ciphertext is not None:
            try:
                cipher = self.credential_cipher or ProviderCredentialCipher.from_settings(
                    self.settings
                )
                secret = cipher.decrypt(
                    credential.secret_ciphertext
                )
            except CredentialEncryptionError as exc:
                raise ModelGatewayError(ModelGatewayErrorCode.MODEL_AUTH_FAILED) from exc
            set_committed_value(credential, "secret", secret)
        elif credential is not None and credential.secret is not None:
            settings = self.settings or get_settings()
            if not settings.credential_allow_legacy_plaintext:
                raise ModelGatewayError(
                    ModelGatewayErrorCode.MODEL_AUTH_FAILED,
                    message="Provider credential migration is required before model execution.",
                )
        return credential

    async def get_model_profile(
        self,
        context: WorkspaceExecutionContext,
        profile_id: UUID,
    ) -> ModelProfile | None:
        workspace_id = _workspace_id(context)
        if workspace_id is None:
            return None
        result = await self.session.execute(
            select(ModelProfile).where(
                ModelProfile.id == profile_id,
                ModelProfile.workspace_id == workspace_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_model_profiles(
        self,
        context: WorkspaceExecutionContext,
    ) -> Sequence[ModelProfile]:
        workspace_id = _workspace_id(context)
        if workspace_id is None:
            return ()
        result = await self.session.execute(
            select(ModelProfile)
            .where(ModelProfile.workspace_id == workspace_id)
            .order_by(ModelProfile.created_at, ModelProfile.id)
        )
        return result.scalars().all()
