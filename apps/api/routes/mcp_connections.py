"""Workspace-scoped HTTP surface for remote MCP connections.

These live on their own prefix rather than under ``/tools``. A connection is
not a tool and does not become one in 3A: it is a server AgentHub may talk to,
and the catalog it returns is something a human still has to promote.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.mcp_connections import (
    McpConnectionCreateRequest,
    McpConnectionDiscoveryResponse,
    McpConnectionPatchRequest,
    McpConnectionResponse,
    McpConnectionRotateSecretRequest,
    McpConnectionTestResponse,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.mcp.service import McpConnectionService

router = APIRouter(tags=["mcp-connections"])
db_session_dependency = Depends(get_db_session)
context_dependency = Depends(get_workspace_context)
service = McpConnectionService()

PREFIX = "/api/v1/workspaces/{workspace_id}/mcp-connections"


@router.get(PREFIX, response_model=list[McpConnectionResponse])
async def list_mcp_connections(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[McpConnectionResponse]:
    del workspace_id
    return [
        McpConnectionResponse.model_validate(item)
        for item in await service.list_connections(session, context)
    ]


@router.post(
    PREFIX, response_model=McpConnectionResponse, status_code=status.HTTP_201_CREATED
)
async def create_mcp_connection(
    workspace_id: UUID,
    payload: McpConnectionCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> McpConnectionResponse:
    del workspace_id
    item = await service.create_connection(
        session,
        context,
        name=payload.name,
        endpoint_url=payload.endpoint_url,
        auth_type=payload.auth_type,
        secret=payload.secret,
        enabled=payload.enabled,
    )
    return McpConnectionResponse.model_validate(item)


@router.get(PREFIX + "/{connection_id}", response_model=McpConnectionResponse)
async def get_mcp_connection(
    workspace_id: UUID,
    connection_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> McpConnectionResponse:
    del workspace_id
    return McpConnectionResponse.model_validate(
        await service.get_connection(session, context, connection_id)
    )


@router.patch(PREFIX + "/{connection_id}", response_model=McpConnectionResponse)
async def patch_mcp_connection(
    workspace_id: UUID,
    connection_id: UUID,
    payload: McpConnectionPatchRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> McpConnectionResponse:
    del workspace_id
    item = await service.patch_connection(
        session, context, connection_id, payload.model_dump(exclude_unset=True)
    )
    return McpConnectionResponse.model_validate(item)


@router.post(PREFIX + "/{connection_id}/rotate-secret", response_model=McpConnectionResponse)
async def rotate_mcp_connection_secret(
    workspace_id: UUID,
    connection_id: UUID,
    payload: McpConnectionRotateSecretRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> McpConnectionResponse:
    del workspace_id
    item = await service.rotate_secret(session, context, connection_id, payload.secret)
    return McpConnectionResponse.model_validate(item)


@router.post(PREFIX + "/{connection_id}/test", response_model=McpConnectionTestResponse)
async def test_mcp_connection(
    workspace_id: UUID,
    connection_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> McpConnectionTestResponse:
    """Report whether the remote is reachable and speaks MCP.

    A remote that is down, slow, or rejecting the token is an answer about the
    remote, not an error in this request: those come back 200 with a status of
    ``unavailable`` and a normalized failure code. Authorization, validation
    and lifecycle problems remain ordinary 4xx.
    """

    del workspace_id
    return McpConnectionTestResponse.model_validate(
        await service.test_connection(session, context, connection_id)
    )


@router.post(
    PREFIX + "/{connection_id}/discover-tools",
    response_model=McpConnectionDiscoveryResponse,
)
async def discover_mcp_connection_tools(
    workspace_id: UUID,
    connection_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> McpConnectionDiscoveryResponse:
    """Read the remote tool catalog. Nothing is imported and nothing persisted."""

    del workspace_id
    return McpConnectionDiscoveryResponse.model_validate(
        await service.discover_tools(session, context, connection_id)
    )


__all__ = ["router"]
