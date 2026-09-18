from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_dependencies import get_current_principal
from apps.api.dependencies import get_db_session
from apps.api.schemas.citation_qa import CitationQaRequest
from packages.control_plane.services import TenantService
from packages.core.execution_context.models import PrincipalContext, WorkspaceExecutionContext
from packages.knowledge.adapters.celery_queue import CeleryIngestionQueue
from packages.knowledge.composition import (
    RetrievalComponents,
    production_retrieval_components,
)
from packages.knowledge.queue import IngestionQueue
from packages.model_gateway.contracts import ModelGateway
from packages.model_gateway.gateway import SqlAlchemyModelGateway

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


async def get_ingestion_queue(request: Request) -> IngestionQueue:
    queue = getattr(request.app.state, "ingestion_queue", None)
    if queue is None:
        from apps.worker.celery_app import create_celery_app

        queue = CeleryIngestionQueue(create_celery_app(request.app.state.settings))
        request.app.state.ingestion_queue = queue
    return queue


async def get_retrieval_components(request: Request) -> RetrievalComponents:
    """Use real adapters in production; tests override this dependency explicitly."""

    return production_retrieval_components(request.app.state.settings)


async def get_query_workspace_context(
    payload: CitationQaRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> WorkspaceExecutionContext:
    return (
        await TenantService().get_workspace_access(
            session,
            principal=principal,
            workspace_id=payload.workspace_id,
        )
    ).context


async def get_model_gateway(session: AsyncSession = db_session_dependency) -> ModelGateway:
    return SqlAlchemyModelGateway(session)
