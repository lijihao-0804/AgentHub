from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_model_gateway, get_workspace_context
from apps.api.schemas.product_control_plane import (
    AgentKnowledgeBindingRequest,
    AgentKnowledgeBindingResponse,
    AgentToolBindingRequest,
    AgentToolBindingResponse,
    KnowledgeDocumentManagementResponse,
    KnowledgeSnapshotDetailResponse,
    KnowledgeSnapshotManagementResponse,
    ModelProfileCreateRequest,
    ModelProfilePatchRequest,
    ModelProfileResponse,
    ModelProfileTestResponse,
    ProviderCredentialCreateRequest,
    ProviderCredentialPatchRequest,
    ProviderCredentialResponse,
    ProviderCredentialRotateSecretRequest,
    ToolCatalogEntryResponse,
    ToolCreateRequest,
    ToolPatchRequest,
    ToolResponse,
    ToolRevisionResponse,
)
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.contracts import ModelGateway

router = APIRouter(tags=["product-control-plane"])
db_session_dependency = Depends(get_db_session)
context_dependency = Depends(get_workspace_context)
model_gateway_dependency = Depends(get_model_gateway)
service = ProductControlPlaneService()


@router.get(
    "/api/v1/workspaces/{workspace_id}/provider-credentials",
    response_model=list[ProviderCredentialResponse],
)
async def list_provider_credentials(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[ProviderCredentialResponse]:
    del workspace_id
    return [
        ProviderCredentialResponse.model_validate(item)
        for item in await service.list_provider_credentials(session, context)
    ]


@router.post(
    "/api/v1/workspaces/{workspace_id}/provider-credentials",
    response_model=ProviderCredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider_credential(
    workspace_id: UUID,
    payload: ProviderCredentialCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ProviderCredentialResponse:
    del workspace_id
    item = await service.create_provider_credential(
        session,
        context,
        provider=payload.provider,
        name=payload.name,
        secret=payload.secret,
        base_url=payload.base_url,
        enabled=payload.enabled,
    )
    return ProviderCredentialResponse.model_validate(item)


@router.get(
    "/api/v1/workspaces/{workspace_id}/provider-credentials/{credential_id}",
    response_model=ProviderCredentialResponse,
)
async def get_provider_credential(
    workspace_id: UUID,
    credential_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ProviderCredentialResponse:
    del workspace_id
    return ProviderCredentialResponse.model_validate(
        await service.get_provider_credential(session, context, credential_id)
    )


@router.patch(
    "/api/v1/workspaces/{workspace_id}/provider-credentials/{credential_id}",
    response_model=ProviderCredentialResponse,
)
async def patch_provider_credential(
    workspace_id: UUID,
    credential_id: UUID,
    payload: ProviderCredentialPatchRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ProviderCredentialResponse:
    del workspace_id
    item = await service.patch_provider_credential(
        session, context, credential_id, payload.model_dump(exclude_unset=True)
    )
    return ProviderCredentialResponse.model_validate(item)


@router.post(
    "/api/v1/workspaces/{workspace_id}/provider-credentials/{credential_id}/rotate-secret",
    response_model=ProviderCredentialResponse,
)
async def rotate_provider_secret(
    workspace_id: UUID,
    credential_id: UUID,
    payload: ProviderCredentialRotateSecretRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ProviderCredentialResponse:
    del workspace_id
    item = await service.rotate_provider_secret(session, context, credential_id, payload.secret)
    return ProviderCredentialResponse.model_validate(item)


@router.get(
    "/api/v1/workspaces/{workspace_id}/model-profiles",
    response_model=list[ModelProfileResponse],
)
async def list_model_profiles(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[ModelProfileResponse]:
    del workspace_id
    return [
        ModelProfileResponse.model_validate(item, from_attributes=True)
        for item in await service.list_model_profiles(session, context)
    ]


@router.post(
    "/api/v1/workspaces/{workspace_id}/model-profiles",
    response_model=ModelProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_profile(
    workspace_id: UUID,
    payload: ModelProfileCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ModelProfileResponse:
    del workspace_id
    return ModelProfileResponse.model_validate(
        await service.create_model_profile(session, context, payload.model_dump()),
        from_attributes=True,
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/model-profiles/{profile_id}",
    response_model=ModelProfileResponse,
)
async def get_model_profile(
    workspace_id: UUID,
    profile_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ModelProfileResponse:
    del workspace_id
    return ModelProfileResponse.model_validate(
        await service.get_model_profile(session, context, profile_id), from_attributes=True
    )


@router.patch(
    "/api/v1/workspaces/{workspace_id}/model-profiles/{profile_id}",
    response_model=ModelProfileResponse,
)
async def patch_model_profile(
    workspace_id: UUID,
    profile_id: UUID,
    payload: ModelProfilePatchRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ModelProfileResponse:
    del workspace_id
    return ModelProfileResponse.model_validate(
        await service.patch_model_profile(
            session, context, profile_id, payload.model_dump(exclude_unset=True)
        ),
        from_attributes=True,
    )


@router.post(
    "/api/v1/workspaces/{workspace_id}/model-profiles/{profile_id}/test",
    response_model=ModelProfileTestResponse,
)
async def test_model_profile(
    workspace_id: UUID,
    profile_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
    gateway: ModelGateway = model_gateway_dependency,
) -> ModelProfileTestResponse:
    del workspace_id
    return ModelProfileTestResponse.model_validate(
        await service.test_model_profile(session, context, profile_id, gateway)
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/tool-catalog",
    response_model=list[ToolCatalogEntryResponse],
)
async def list_tool_catalog(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[ToolCatalogEntryResponse]:
    del workspace_id
    return [
        ToolCatalogEntryResponse.model_validate(item)
        for item in await service.list_tool_catalog(context)
    ]


@router.get(
    "/api/v1/workspaces/{workspace_id}/tools",
    response_model=list[ToolResponse],
)
async def list_tools(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[ToolResponse]:
    del workspace_id
    return [
        ToolResponse.model_validate(item)
        for item in await service.list_tools(session, context)
    ]


@router.post(
    "/api/v1/workspaces/{workspace_id}/tools",
    response_model=ToolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool(
    workspace_id: UUID,
    payload: ToolCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ToolResponse:
    del workspace_id
    return ToolResponse.model_validate(
        await service.create_tool(
            session,
            context,
            payload.resolved_identity,
            name=payload.name,
            description=payload.description,
        )
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/tools/{tool_id}",
    response_model=ToolResponse,
)
async def get_tool(
    workspace_id: UUID,
    tool_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ToolResponse:
    del workspace_id
    return ToolResponse.model_validate(await service.get_tool(session, context, tool_id))


@router.patch(
    "/api/v1/workspaces/{workspace_id}/tools/{tool_id}",
    response_model=ToolResponse,
)
async def patch_tool(
    workspace_id: UUID,
    tool_id: UUID,
    payload: ToolPatchRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ToolResponse:
    del workspace_id
    return ToolResponse.model_validate(
        await service.patch_tool(session, context, tool_id, payload.model_dump(exclude_unset=True))
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/tools/{tool_id}/revisions",
    response_model=list[ToolRevisionResponse],
)
async def list_tool_revisions(
    workspace_id: UUID,
    tool_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[ToolRevisionResponse]:
    del workspace_id
    revisions = await service.list_tool_revisions(session, context, tool_id)
    return [ToolRevisionResponse.model_validate(item, from_attributes=True) for item in revisions]


@router.get(
    "/api/v1/workspaces/{workspace_id}/tools/{tool_id}/revisions/{revision_id}",
    response_model=ToolRevisionResponse,
)
async def get_tool_revision(
    workspace_id: UUID,
    tool_id: UUID,
    revision_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> ToolRevisionResponse:
    del workspace_id
    return ToolRevisionResponse.model_validate(
        await service.get_tool_revision(session, context, tool_id, revision_id),
        from_attributes=True,
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/knowledge-bindings",
    response_model=list[AgentKnowledgeBindingResponse],
)
async def get_agent_knowledge_bindings(
    workspace_id: UUID,
    agent_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[AgentKnowledgeBindingResponse]:
    del workspace_id
    return [
        AgentKnowledgeBindingResponse.model_validate(item, from_attributes=True)
        for item in await service.get_knowledge_bindings(session, context, agent_id)
    ]


@router.put(
    "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/knowledge-bindings",
    response_model=list[AgentKnowledgeBindingResponse],
)
async def replace_agent_knowledge_bindings(
    workspace_id: UUID,
    agent_id: UUID,
    payload: list[AgentKnowledgeBindingRequest],
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[AgentKnowledgeBindingResponse]:
    del workspace_id
    bindings = await service.replace_knowledge_bindings(
        session, context, agent_id, [item.model_dump() for item in payload]
    )
    return [
        AgentKnowledgeBindingResponse.model_validate(item, from_attributes=True)
        for item in bindings
    ]


@router.get(
    "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/tool-bindings",
    response_model=list[AgentToolBindingResponse],
)
async def get_agent_tool_bindings(
    workspace_id: UUID,
    agent_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[AgentToolBindingResponse]:
    del workspace_id
    return [
        AgentToolBindingResponse.model_validate(item, from_attributes=True)
        for item in await service.get_tool_bindings(session, context, agent_id)
    ]


@router.put(
    "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/tool-bindings",
    response_model=list[AgentToolBindingResponse],
)
async def replace_agent_tool_bindings(
    workspace_id: UUID,
    agent_id: UUID,
    payload: list[AgentToolBindingRequest],
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[AgentToolBindingResponse]:
    del workspace_id
    bindings = await service.replace_tool_bindings(
        session, context, agent_id, [item.model_dump() for item in payload]
    )
    return [
        AgentToolBindingResponse.model_validate(item, from_attributes=True)
        for item in bindings
    ]


@router.get(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents",
    response_model=list[KnowledgeDocumentManagementResponse],
)
async def list_knowledge_documents(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[KnowledgeDocumentManagementResponse]:
    del workspace_id
    return [
        KnowledgeDocumentManagementResponse.model_validate(item)
        for item in await service.list_documents(session, context, knowledge_base_id)
    ]


@router.get(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/snapshots",
    response_model=list[KnowledgeSnapshotManagementResponse],
)
async def list_knowledge_snapshots(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[KnowledgeSnapshotManagementResponse]:
    del workspace_id
    return [
        KnowledgeSnapshotManagementResponse.model_validate(item)
        for item in await service.list_snapshots(session, context, knowledge_base_id)
    ]


@router.get(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/snapshots/{snapshot_id}",
    response_model=KnowledgeSnapshotDetailResponse,
)
async def get_knowledge_snapshot(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    snapshot_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> KnowledgeSnapshotDetailResponse:
    del workspace_id
    return KnowledgeSnapshotDetailResponse.model_validate(
        await service.get_snapshot(session, context, knowledge_base_id, snapshot_id)
    )


__all__ = ["router"]
