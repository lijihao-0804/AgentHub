from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from apps.api.knowledge_dependencies import get_agent_run_workspace_context
from apps.api.schemas.artifacts import (
    ArtifactCreateRequest,
    ArtifactPatchRequest,
    ArtifactResponse,
)
from packages.artifacts.service import ArtifactService
from packages.core.execution_context.models import WorkspaceExecutionContext

PREFIX = "/api/v1/workspaces/{workspace_id}"

router = APIRouter(prefix=PREFIX, tags=["artifacts"])
context_dependency = Depends(get_agent_run_workspace_context)
type_query = Query(default=None, max_length=64)
limit_query = Query(default=50, ge=1, le=200)
offset_query = Query(default=0, ge=0)


def _service(request: Request) -> ArtifactService:
    return ArtifactService(get_production_agent_run_service(request).session_factory)


@router.get("/threads/{thread_id}/artifacts", response_model=list[ArtifactResponse])
async def list_thread_artifacts(
    workspace_id: UUID,
    thread_id: UUID,
    request: Request,
    type: str | None = type_query,
    limit: int = limit_query,
    offset: int = offset_query,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[ArtifactResponse]:
    del workspace_id
    artifacts = await _service(request).list_for_thread(
        context, thread_id, artifact_type=type, limit=limit, offset=offset
    )
    return [ArtifactResponse.model_validate(artifact) for artifact in artifacts]


@router.post(
    "/threads/{thread_id}/artifacts",
    response_model=ArtifactResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_thread_artifact(
    workspace_id: UUID,
    thread_id: UUID,
    payload: ArtifactCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ArtifactResponse:
    del workspace_id
    artifact = await _service(request).create(
        context,
        thread_id,
        artifact_type=payload.type,
        title=payload.title,
        content=payload.content,
    )
    return ArtifactResponse.model_validate(artifact)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(
    workspace_id: UUID,
    artifact_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ArtifactResponse:
    del workspace_id
    return ArtifactResponse.model_validate(await _service(request).get(context, artifact_id))


@router.patch("/artifacts/{artifact_id}", response_model=ArtifactResponse)
async def patch_artifact(
    workspace_id: UUID,
    artifact_id: UUID,
    payload: ArtifactPatchRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ArtifactResponse:
    del workspace_id
    artifact = await _service(request).update(
        context, artifact_id, title=payload.title, content=payload.content
    )
    return ArtifactResponse.model_validate(artifact)


@router.delete("/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact(
    workspace_id: UUID,
    artifact_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> Response:
    del workspace_id
    await _service(request).delete(context, artifact_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
