from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.agents import (
    AgentCreateRequest,
    AgentMemoryListResponse,
    AgentMemoryResponse,
    AgentPatchRequest,
    AgentPreflightResponse,
    AgentPublishResponse,
    AgentResponse,
    AgentVersionResponse,
)
from packages.agent_runtime.models import Agent
from packages.agent_runtime.publish import AgentPublishService
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.memory.models import WorkspaceMemory
from packages.memory.service import MAX_PAGE_SIZE, MAX_SEARCH_LENGTH, MemoryAdminService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/agents", tags=["agents"])
db_session_dependency = Depends(get_db_session)
context_dependency = Depends(get_workspace_context)


MemoryStatusFilter = Literal["ACTIVE", "SUPERSEDED", "INVALIDATED"]
MemoryKindFilter = Literal["FACT", "PREFERENCE", "DECISION", "CONSTRAINT"]
memory_status_query = Query(default=None, alias="status")
memory_kind_query = Query(default=None, alias="kind")
memory_search_query = Query(default=None, alias="q", max_length=MAX_SEARCH_LENGTH)
memory_limit_query = Query(default=50, ge=1, le=MAX_PAGE_SIZE)
memory_offset_query = Query(default=0, ge=0)


def _agent_response(agent: Agent) -> AgentResponse:
    return AgentResponse.model_validate(agent, from_attributes=True)


def _memory_response(memory: WorkspaceMemory) -> AgentMemoryResponse:
    return AgentMemoryResponse.model_validate(memory, from_attributes=True)


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    workspace_id: UUID,
    payload: AgentCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentResponse:
    del workspace_id
    agent = await AgentPublishService().create_draft(
        session,
        context,
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        prompt_version=payload.prompt_version,
        model_profile_id=payload.model_profile_id,
        knowledge_binding_mode=payload.knowledge_binding_mode,
        model_retry_policy=payload.model_retry_policy.model_dump(),
        retrieval_config=payload.retrieval_config.model_dump(exclude_none=True),
        runtime_config=payload.runtime_config.model_dump(exclude_none=True),
    )
    return _agent_response(agent)


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[AgentResponse]:
    del workspace_id
    agents = await AgentPublishService().list_drafts(session, context)
    return [_agent_response(agent) for agent in agents]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    workspace_id: UUID,
    agent_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentResponse:
    del workspace_id
    agent = await AgentPublishService().get_draft(session, context, agent_id)
    return _agent_response(agent)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    workspace_id: UUID,
    agent_id: UUID,
    payload: AgentPatchRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentResponse:
    del workspace_id
    values = payload.model_dump(exclude_unset=True)
    agent = await AgentPublishService().update_draft(session, context, agent_id, values)
    return _agent_response(agent)


@router.post("/{agent_id}/publish", response_model=AgentPublishResponse)
async def publish_agent(
    workspace_id: UUID,
    agent_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentPublishResponse:
    del workspace_id
    published = await AgentPublishService().publish(session, context, agent_id)
    return AgentPublishResponse(
        agent_version_id=published.id,
        agent_id=published.agent_id,
        version_number=published.version_number,
        resolved_spec_hash=published.resolved_spec_hash,
        created_at=published.created_at,
    )


@router.post("/{agent_id}/preflight", response_model=AgentPreflightResponse)
async def preflight_agent(
    workspace_id: UUID,
    agent_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentPreflightResponse:
    del workspace_id
    return AgentPreflightResponse.model_validate(
        await AgentPublishService().preflight(session, context, agent_id)
    )


@router.get("/{agent_id}/versions", response_model=list[AgentVersionResponse])
async def list_agent_versions(
    workspace_id: UUID,
    agent_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[AgentVersionResponse]:
    del workspace_id
    versions = await AgentPublishService().list_versions(session, context, agent_id)
    return [
        AgentVersionResponse.model_validate(version, from_attributes=True)
        for version in versions
    ]


@router.get(
    "/{agent_id}/versions/{version_id}",
    response_model=AgentVersionResponse,
)
async def get_agent_version(
    workspace_id: UUID,
    agent_id: UUID,
    version_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentVersionResponse:
    del workspace_id
    return AgentVersionResponse.model_validate(
        await AgentPublishService().get_version(session, context, agent_id, version_id),
        from_attributes=True,
    )


__all__ = ["router"]


@router.get("/{agent_id}/memories", response_model=AgentMemoryListResponse)
async def list_agent_memories(
    workspace_id: UUID,
    agent_id: UUID,
    status_filter: MemoryStatusFilter | None = memory_status_query,
    kind_filter: MemoryKindFilter | None = memory_kind_query,
    search: str | None = memory_search_query,
    limit: int = memory_limit_query,
    offset: int = memory_offset_query,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentMemoryListResponse:
    del workspace_id
    page = await MemoryAdminService().list_for_agent(
        session,
        context,
        agent_id=agent_id,
        status=status_filter,
        kind=kind_filter,
        search=search,
        limit=limit,
        offset=offset,
    )
    return AgentMemoryListResponse(
        items=[_memory_response(memory) for memory in page.items], total=page.total
    )


@router.post("/{agent_id}/memories/{memory_id}/invalidate", response_model=AgentMemoryResponse)
async def invalidate_agent_memory(
    workspace_id: UUID,
    agent_id: UUID,
    memory_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentMemoryResponse:
    """Stop using this memory. Not a delete -- see ADR-011."""

    del workspace_id
    memory = await MemoryAdminService().invalidate(
        session, context, agent_id=agent_id, memory_id=memory_id
    )
    return _memory_response(memory)


@router.post("/{agent_id}/memories/{memory_id}/reactivate", response_model=AgentMemoryResponse)
async def reactivate_agent_memory(
    workspace_id: UUID,
    agent_id: UUID,
    memory_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> AgentMemoryResponse:
    del workspace_id
    memory = await MemoryAdminService().reactivate(
        session, context, agent_id=agent_id, memory_id=memory_id
    )
    return _memory_response(memory)
