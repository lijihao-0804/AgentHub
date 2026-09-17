"""Workspace-scoped persistence for model credentials and profiles."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.models import ModelProfile, ProviderCredential


def _workspace_id(context: WorkspaceExecutionContext) -> UUID | None:
    try:
        return UUID(context.workspace_id)
    except ValueError:
        return None


class SqlAlchemyModelGatewayRepository:
    """Repository methods intentionally require execution context for every lookup."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
        return result.scalar_one_or_none()

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
