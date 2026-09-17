from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_dependencies import get_current_principal
from apps.api.dependencies import get_db_session
from apps.api.schemas.tenant import (
    OrganizationCreateRequest,
    OrganizationMemberCreateRequest,
    OrganizationMemberResponse,
    OrganizationMemberUpdateRequest,
    OrganizationResponse,
    WorkspaceCreateRequest,
    WorkspaceMemberCreateRequest,
    WorkspaceMemberResponse,
    WorkspaceMemberUpdateRequest,
    WorkspaceResponse,
)
from packages.control_plane.models import User
from packages.control_plane.services import TenantService
from packages.core.execution_context.models import PrincipalContext

router = APIRouter(tags=["tenant"])
db_session_dependency = Depends(get_db_session)
principal_dependency = Depends(get_current_principal)


@router.post(
    "/api/v1/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    payload: OrganizationCreateRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> OrganizationResponse:
    organization = await TenantService().create_organization(
        session, principal=principal, name=payload.name
    )
    return OrganizationResponse(id=organization.id, name=organization.name)


@router.get("/api/v1/organizations", response_model=list[OrganizationResponse])
async def list_organizations(
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[OrganizationResponse]:
    organizations = await TenantService().list_organizations(session, principal=principal)
    return [OrganizationResponse(id=item.id, name=item.name) for item in organizations]


@router.get(
    "/api/v1/organizations/{organization_id}/members",
    response_model=list[OrganizationMemberResponse],
)
async def list_organization_members(
    organization_id: UUID,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[OrganizationMemberResponse]:
    members = await TenantService().list_organization_members(
        session, principal=principal, organization_id=organization_id
    )
    return [
        OrganizationMemberResponse(user_id=user.id, email=user.email, role=membership.role)
        for membership, user in members
    ]


@router.post(
    "/api/v1/organizations/{organization_id}/members",
    response_model=OrganizationMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_organization_member(
    organization_id: UUID,
    payload: OrganizationMemberCreateRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> OrganizationMemberResponse:
    membership = await TenantService().add_organization_member(
        session,
        principal=principal,
        organization_id=organization_id,
        user_id=payload.user_id,
        role=payload.role,
    )
    user = await session.get(User, payload.user_id)
    return OrganizationMemberResponse(
        user_id=payload.user_id,
        email=user.email,
        role=membership.role,
    )


@router.patch(
    "/api/v1/organizations/{organization_id}/members/{user_id}",
    response_model=OrganizationMemberResponse,
)
async def update_organization_member(
    organization_id: UUID,
    user_id: UUID,
    payload: OrganizationMemberUpdateRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> OrganizationMemberResponse:
    membership = await TenantService().change_organization_member_role(
        session,
        principal=principal,
        organization_id=organization_id,
        target_user_id=user_id,
        new_role=payload.role,
    )
    user = await session.get(User, user_id)
    return OrganizationMemberResponse(user_id=user_id, email=user.email, role=membership.role)


@router.delete("/api/v1/organizations/{organization_id}/members/{user_id}", status_code=204)
async def remove_organization_member(
    organization_id: UUID,
    user_id: UUID,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> None:
    await TenantService().remove_organization_member(
        session,
        principal=principal,
        organization_id=organization_id,
        target_user_id=user_id,
    )


@router.post(
    "/api/v1/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> WorkspaceResponse:
    workspace = await TenantService().create_workspace(
        session,
        principal=principal,
        organization_id=payload.organization_id,
        name=payload.name,
    )
    return WorkspaceResponse(
        id=workspace.id,
        organization_id=workspace.organization_id,
        name=workspace.name,
    )


@router.get("/api/v1/workspaces", response_model=list[WorkspaceResponse])
async def list_workspaces(
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[WorkspaceResponse]:
    workspaces = await TenantService().list_workspaces(session, principal=principal)
    return [
        WorkspaceResponse(id=item.id, organization_id=item.organization_id, name=item.name)
        for item in workspaces
    ]


@router.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: UUID,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> WorkspaceResponse:
    access = await TenantService().get_workspace_access(
        session, principal=principal, workspace_id=workspace_id
    )
    return WorkspaceResponse(
        id=access.workspace.id,
        organization_id=access.workspace.organization_id,
        name=access.workspace.name,
    )


@router.get(
    "/api/v1/workspaces/{workspace_id}/members",
    response_model=list[WorkspaceMemberResponse],
)
async def list_workspace_members(
    workspace_id: UUID,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[WorkspaceMemberResponse]:
    members = await TenantService().list_workspace_members(
        session, principal=principal, workspace_id=workspace_id
    )
    return [
        WorkspaceMemberResponse(user_id=user.id, email=user.email, role=membership.role)
        for membership, user in members
    ]


@router.post(
    "/api/v1/workspaces/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_member(
    workspace_id: UUID,
    payload: WorkspaceMemberCreateRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> WorkspaceMemberResponse:
    membership = await TenantService().add_workspace_member(
        session,
        principal=principal,
        workspace_id=workspace_id,
        user_id=payload.user_id,
        role=payload.role,
    )
    user = await session.get(User, payload.user_id)
    return WorkspaceMemberResponse(user_id=payload.user_id, email=user.email, role=membership.role)


@router.patch(
    "/api/v1/workspaces/{workspace_id}/members/{user_id}",
    response_model=WorkspaceMemberResponse,
)
async def update_workspace_member(
    workspace_id: UUID,
    user_id: UUID,
    payload: WorkspaceMemberUpdateRequest,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> WorkspaceMemberResponse:
    membership = await TenantService().update_workspace_member(
        session,
        principal=principal,
        workspace_id=workspace_id,
        user_id=user_id,
        role=payload.role,
    )
    user = await session.get(User, user_id)
    return WorkspaceMemberResponse(user_id=user_id, email=user.email, role=membership.role)


@router.delete("/api/v1/workspaces/{workspace_id}/members/{user_id}", status_code=204)
async def remove_workspace_member(
    workspace_id: UUID,
    user_id: UUID,
    principal: PrincipalContext = principal_dependency,
    session: AsyncSession = db_session_dependency,
) -> None:
    await TenantService().remove_workspace_member(
        session,
        principal=principal,
        workspace_id=workspace_id,
        user_id=user_id,
    )
