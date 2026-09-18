from __future__ import annotations

from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_dependencies import get_current_principal
from apps.api.dependencies import get_db_session
from packages.control_plane.services import TenantService
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext

db_session_dependency = Depends(get_db_session)
principal_dependency = Depends(get_current_principal)


async def get_workspace_context(
    workspace_id: UUID,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> WorkspaceExecutionContext:
    return (
        await TenantService().get_workspace_access(
            session, principal=principal, workspace_id=workspace_id
        )
    ).context
