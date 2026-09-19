from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from apps.api.knowledge_dependencies import get_agent_run_workspace_context
from apps.api.schemas.runs import RunDetail, RunListResponse, RunTimelineResponse
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.observability.runs import RunQueryService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["runs"])
context_dependency = Depends(get_agent_run_workspace_context)


def _service(request: Request) -> RunQueryService:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise AgentHubError("DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503)
    return RunQueryService(factory)


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    workspace_id: UUID,
    request: Request,
    status: Annotated[str | None, Query()] = None,
    agent_version_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    context: WorkspaceExecutionContext = context_dependency,
) -> RunListResponse:
    del workspace_id
    items, next_cursor = await _service(request).list_runs(
        context,
        status=status,
        agent_version_id=agent_version_id,
        limit=limit,
        cursor=cursor,
    )
    return RunListResponse(items=items, next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run_detail(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> RunDetail:
    del workspace_id
    return RunDetail.model_validate(await _service(request).get_detail(context, run_id))


@router.get("/runs/{run_id}/steps", response_model=RunTimelineResponse)
async def get_run_timeline(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> RunTimelineResponse:
    del workspace_id
    items = await _service(request).get_timeline(context, run_id)
    return RunTimelineResponse(run_id=run_id, workspace_id=context.workspace_id, items=items)


__all__ = ["router"]
