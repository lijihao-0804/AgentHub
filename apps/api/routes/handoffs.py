from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from apps.api.knowledge_dependencies import get_agent_run_workspace_context
from apps.api.schemas.handoffs import (
    AssigneeResponse,
    AssignRequest,
    CloseRequest,
    HandoffResponse,
    VersionRequest,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.handoffs.service import HandoffService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["handoffs"])
context_dependency = Depends(get_agent_run_workspace_context)
limit_query = Query(default=50, ge=1, le=200)
offset_query = Query(default=0, ge=0)


def service(request):
    return HandoffService(get_production_agent_run_service(request).session_factory)


@router.get("/artifacts/{artifact_id}/handoff", response_model=HandoffResponse | None)
async def get_artifact_handoff(
    workspace_id: UUID,
    artifact_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
):
    return await service(request).get_for_artifact(context, artifact_id)


@router.post("/artifacts/{artifact_id}/handoff", response_model=HandoffResponse)
async def open_handoff(
    workspace_id: UUID,
    artifact_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
):
    return await service(request).open(context, artifact_id)


@router.get("/threads/{thread_id}/handoffs", response_model=list[HandoffResponse])
async def list_handoffs(
    workspace_id: UUID,
    thread_id: UUID,
    request: Request,
    limit: int = limit_query,
    offset: int = offset_query,
    context: WorkspaceExecutionContext = context_dependency,
):
    return await service(request).list_for_thread(context, thread_id, limit=limit, offset=offset)


@router.get("/handoffs/assignees", response_model=list[AssigneeResponse])
async def list_assignees(
    workspace_id: UUID, request: Request, context: WorkspaceExecutionContext = context_dependency
):
    return await service(request).assignees(context)


@router.post("/handoffs/{case_id}/assign", response_model=HandoffResponse)
async def assign_handoff(
    workspace_id: UUID,
    case_id: UUID,
    payload: AssignRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
):
    return await service(request).assign(context, case_id, **payload.model_dump())


@router.post("/handoffs/{case_id}/claim", response_model=HandoffResponse)
async def claim_handoff(
    workspace_id: UUID,
    case_id: UUID,
    payload: VersionRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
):
    return await service(request).claim(context, case_id, **payload.model_dump())


@router.post("/handoffs/{case_id}/close", response_model=HandoffResponse)
async def close_handoff(
    workspace_id: UUID,
    case_id: UUID,
    payload: CloseRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
):
    return await service(request).close(context, case_id, **payload.model_dump())
