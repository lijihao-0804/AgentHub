from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from apps.api.knowledge_dependencies import get_agent_run_workspace_context
from apps.api.schemas.observability import (
    AgentVersionBreakdownResponse,
    FailureAnalyticsResponse,
    ObservabilitySummary,
    TimeseriesResponse,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.observability.metrics import MetricsQueryService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/observability", tags=["observability"])
context_dependency = Depends(get_agent_run_workspace_context)


def _service(request: Request) -> MetricsQueryService:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise AgentHubError("DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503)
    return MetricsQueryService(factory)


@router.get("/summary", response_model=ObservabilitySummary)
async def summary(
    workspace_id: UUID,
    request: Request,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    agent_version_id: Annotated[UUID | None, Query()] = None,
    context: WorkspaceExecutionContext = context_dependency,
) -> ObservabilitySummary:
    del workspace_id
    return ObservabilitySummary.model_validate(
        await _service(request).summary(
            context,
            start=from_,
            end=to,
            agent_version_id=agent_version_id,
        )
    )


@router.get("/timeseries", response_model=TimeseriesResponse)
async def timeseries(
    workspace_id: UUID,
    request: Request,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    bucket: Annotated[str, Query()] = "day",
    agent_version_id: Annotated[UUID | None, Query()] = None,
    context: WorkspaceExecutionContext = context_dependency,
) -> TimeseriesResponse:
    del workspace_id
    return TimeseriesResponse.model_validate(
        await _service(request).timeseries(
            context,
            start=from_,
            end=to,
            bucket=bucket,
            agent_version_id=agent_version_id,
        )
    )


@router.get("/failures", response_model=FailureAnalyticsResponse)
async def failures(
    workspace_id: UUID,
    request: Request,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    agent_version_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    context: WorkspaceExecutionContext = context_dependency,
) -> FailureAnalyticsResponse:
    del workspace_id
    return FailureAnalyticsResponse.model_validate(
        await _service(request).failure_analytics(
            context,
            start=from_,
            end=to,
            category=category,
            limit=limit,
            agent_version_id=agent_version_id,
        )
    )


@router.get("/agent-versions", response_model=AgentVersionBreakdownResponse)
async def agent_versions(
    workspace_id: UUID,
    request: Request,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    context: WorkspaceExecutionContext = context_dependency,
) -> AgentVersionBreakdownResponse:
    del workspace_id
    return AgentVersionBreakdownResponse.model_validate(
        await _service(request).agent_version_breakdown(context, start=from_, end=to)
    )


__all__ = ["router"]
