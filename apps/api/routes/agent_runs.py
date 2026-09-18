from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.agent_runs import (
    AgentRunCreateRequest,
    AgentRunResponse,
    RunStepResponse,
)
from packages.agent_runtime.runtime import AgentRunService
from packages.core.execution_context.models import WorkspaceExecutionContext

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["agent-runs"])
db_session_dependency = Depends(get_db_session)
context_dependency = Depends(get_workspace_context)


def _service(request: Request) -> AgentRunService:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        from packages.core.errors.exceptions import AgentHubError

        raise AgentHubError("DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503)
    return AgentRunService(factory)


@router.post(
    "/agent-versions/{agent_version_id}/runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_run(
    workspace_id: UUID,
    agent_version_id: UUID,
    payload: AgentRunCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentRunResponse:
    del workspace_id, session
    service = _service(request)
    run = await service.run(
        context,
        agent_version_id=agent_version_id,
        input_text=payload.input_text,
    )
    return AgentRunResponse.model_validate(await service.get_run(context, run.run_id))


@router.get("/agent-runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentRunResponse:
    del workspace_id, session
    return AgentRunResponse.model_validate(await _service(request).get_run(context, run_id))


@router.get("/agent-runs/{run_id}/steps", response_model=list[RunStepResponse])
async def list_agent_run_steps(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[RunStepResponse]:
    del workspace_id, session
    steps = await _service(request).list_steps(context, run_id)
    return [RunStepResponse.model_validate(step) for step in steps]


__all__ = ["router"]
