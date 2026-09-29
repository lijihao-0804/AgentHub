from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.approvals import (
    ApprovalDecisionResponse,
    ApprovalDenyRequest,
    ApprovalListResponse,
    ApprovalResponse,
)
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.core.execution_context.models import WorkspaceExecutionContext

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["approvals"])
context_dependency = Depends(get_workspace_context)


def _approval_service(request: Request) -> ApprovalService:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        from packages.core.errors.exceptions import AgentHubError

        raise AgentHubError("DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503)
    return ApprovalService(factory)


def _agent_run_service(request: Request) -> AgentRunService:
    return get_production_agent_run_service(request)


@router.get("/approvals", response_model=ApprovalListResponse)
async def list_approvals(
    workspace_id: UUID,
    request: Request,
    decision: Literal["PENDING", "DECIDED"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: WorkspaceExecutionContext = context_dependency,
) -> ApprovalListResponse:
    del workspace_id
    items, total = await _approval_service(request).list_page(context, decision, limit, offset)
    return ApprovalListResponse(
        items=[ApprovalResponse.model_validate(approval) for approval in items],
        total=total,
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    workspace_id: UUID,
    approval_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ApprovalResponse:
    del workspace_id
    approval = await _approval_service(request).get(context, approval_id)
    return ApprovalResponse.model_validate(approval)


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalDecisionResponse,
    status_code=status.HTTP_200_OK,
)
async def approve_approval(
    workspace_id: UUID,
    approval_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ApprovalDecisionResponse:
    del workspace_id
    approvals = _approval_service(request)
    approval = await approvals.decide(
        context, approval_id, decision=ApprovalDecisionStatus.APPROVED
    )
    result = await _agent_run_service(request).resume(
        context, run_id=approval.run_id, approval_id=approval.id
    )
    return ApprovalDecisionResponse(
        approval=ApprovalResponse.model_validate(approval),
        run_id=result.run_id,
        run_status=result.status,
    )


@router.post(
    "/approvals/{approval_id}/deny",
    response_model=ApprovalDecisionResponse,
    status_code=status.HTTP_200_OK,
)
async def deny_approval(
    workspace_id: UUID,
    approval_id: UUID,
    request: Request,
    payload: ApprovalDenyRequest | None = None,
    context: WorkspaceExecutionContext = context_dependency,
) -> ApprovalDecisionResponse:
    del workspace_id
    approvals = _approval_service(request)
    approval = await approvals.decide(
        context,
        approval_id,
        decision=ApprovalDecisionStatus.DENIED,
        reason=payload.reason if payload is not None else None,
    )
    result = await _agent_run_service(request).resume(
        context, run_id=approval.run_id, approval_id=approval.id
    )
    return ApprovalDecisionResponse(
        approval=ApprovalResponse.model_validate(approval),
        run_id=result.run_id,
        run_status=result.status,
    )


__all__ = ["router"]
