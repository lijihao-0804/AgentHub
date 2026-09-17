from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.audit import append_audit
from packages.control_plane.enums import OrganizationRole, WorkspaceRole
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.control_plane.rbac import (
    WORKSPACE_ADMIN,
    ensure_not_last_owner,
    has_permission,
    resolve_permissions,
)
from packages.control_plane.repositories import SqlAlchemyTenantRepository
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)


@dataclass(frozen=True)
class WorkspaceAccess:
    workspace: Workspace
    context: WorkspaceExecutionContext


class TenantService:
    async def create_organization(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        name: str,
    ) -> Organization:
        user_id = self._user_id(principal)
        organization = Organization(name=name.strip(), created_by=user_id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user_id,
                role=OrganizationRole.OWNER,
            )
        )
        append_audit(
            session,
            action="organization_create",
            resource_type="organization",
            resource_id=str(organization.id),
            request_id=principal.request_id,
            actor_user_id=user_id,
            organization_id=organization.id,
            safe_metadata={"outcome": "success"},
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "ORGANIZATION_CREATE_FAILED", "Organization could not be created.", 409
            ) from exc
        return organization

    async def list_organizations(
        self, session: AsyncSession, *, principal: PrincipalContext
    ) -> list[Organization]:
        return await SqlAlchemyTenantRepository(session).list_organizations(
            self._user_id(principal)
        )

    async def create_workspace(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        organization_id: UUID,
        name: str,
    ) -> Workspace:
        user_id = self._user_id(principal)
        tenant = SqlAlchemyTenantRepository(session)
        membership = await tenant.get_organization_membership(organization_id, user_id)
        if membership is None or membership.role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            await self._deny(
                session,
                principal=principal,
                resource_type="organization",
                resource_id=str(organization_id),
                organization_id=organization_id,
                operation="workspace_create",
                status_code=404,
            )
        workspace = Workspace(organization_id=organization_id, name=name.strip())
        session.add(workspace)
        await session.flush()
        append_audit(
            session,
            action="workspace_create",
            resource_type="workspace",
            resource_id=str(workspace.id),
            request_id=principal.request_id,
            actor_user_id=user_id,
            organization_id=organization_id,
            workspace_id=workspace.id,
            safe_metadata={"outcome": "success"},
        )
        await session.commit()
        return workspace

    async def list_workspaces(
        self, session: AsyncSession, *, principal: PrincipalContext
    ) -> list[Workspace]:
        return await SqlAlchemyTenantRepository(session).list_workspaces(self._user_id(principal))

    async def get_workspace_access(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        workspace_id: UUID,
    ) -> WorkspaceAccess:
        user_id = self._user_id(principal)
        tenant = SqlAlchemyTenantRepository(session)
        workspace = await tenant.get_workspace(workspace_id)
        if workspace is None:
            raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)
        org_membership = await tenant.get_organization_membership(
            workspace.organization_id, user_id
        )
        if org_membership is None:
            await self._deny(
                session,
                principal=principal,
                resource_type="workspace",
                resource_id=str(workspace_id),
                organization_id=workspace.organization_id,
                workspace_id=workspace_id,
                operation="workspace_access",
                status_code=404,
            )
        if org_membership.role in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            workspace_role = None
        else:
            membership = await tenant.get_workspace_membership(workspace_id, user_id)
            if membership is None:
                await self._deny(
                    session,
                    principal=principal,
                    resource_type="workspace",
                    resource_id=str(workspace_id),
                    organization_id=workspace.organization_id,
                    workspace_id=workspace_id,
                    operation="workspace_access",
                    status_code=404,
                )
            workspace_role = membership.role
        permissions = resolve_permissions(org_membership.role, workspace_role)
        context = WorkspaceExecutionContext(
            organization=OrganizationContext(
                principal=principal,
                organization_id=str(workspace.organization_id),
                org_role=org_membership.role,
            ),
            workspace_id=str(workspace.id),
            workspace_role=workspace_role,
            permissions=permissions,
        )
        return WorkspaceAccess(workspace=workspace, context=context)

    async def list_workspace_members(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        workspace_id: UUID,
    ) -> list[tuple[WorkspaceMembership, User]]:
        await self.get_workspace_access(session, principal=principal, workspace_id=workspace_id)
        return await SqlAlchemyTenantRepository(session).list_workspace_members(workspace_id)

    async def add_workspace_member(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        workspace_id: UUID,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMembership:
        access = await self._require_workspace_admin(
            session, principal, workspace_id, "workspace_member_add"
        )
        if role not in {WorkspaceRole.DEVELOPER, WorkspaceRole.VIEWER}:
            raise AgentHubError("INVALID_WORKSPACE_ROLE", "Workspace role is invalid.", 422)
        tenant = SqlAlchemyTenantRepository(session)
        target_user = await session.get(User, user_id)
        target_org_membership = await tenant.get_organization_membership(
            access.workspace.organization_id, user_id
        )
        if target_user is None or target_org_membership is None:
            raise AgentHubError("USER_NOT_IN_ORGANIZATION", "User is not in the organization.", 404)
        membership = WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role=role)
        session.add(membership)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "WORKSPACE_MEMBER_EXISTS", "User is already a workspace member.", 409
            ) from exc
        append_audit(
            session,
            action="workspace_member_add",
            resource_type="workspace_membership",
            resource_id=f"{workspace_id}:{user_id}",
            request_id=principal.request_id,
            actor_user_id=self._user_id(principal),
            organization_id=access.workspace.organization_id,
            workspace_id=workspace_id,
            safe_metadata={"role": role},
        )
        await session.commit()
        return membership

    async def update_workspace_member(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        workspace_id: UUID,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMembership:
        access = await self._require_workspace_admin(
            session, principal, workspace_id, "workspace_member_update"
        )
        if role not in {WorkspaceRole.DEVELOPER, WorkspaceRole.VIEWER}:
            raise AgentHubError("INVALID_WORKSPACE_ROLE", "Workspace role is invalid.", 422)
        membership = await SqlAlchemyTenantRepository(session).get_workspace_membership(
            workspace_id, user_id, for_update=True
        )
        if membership is None:
            raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)
        membership.role = role
        append_audit(
            session,
            action="workspace_member_update",
            resource_type="workspace_membership",
            resource_id=f"{workspace_id}:{user_id}",
            request_id=principal.request_id,
            actor_user_id=self._user_id(principal),
            organization_id=access.workspace.organization_id,
            workspace_id=workspace_id,
            safe_metadata={"role": role},
        )
        await session.commit()
        return membership

    async def remove_workspace_member(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        workspace_id: UUID,
        user_id: UUID,
    ) -> None:
        access = await self._require_workspace_admin(
            session, principal, workspace_id, "workspace_member_remove"
        )
        membership = await SqlAlchemyTenantRepository(session).get_workspace_membership(
            workspace_id, user_id, for_update=True
        )
        if membership is None:
            raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)
        await session.delete(membership)
        append_audit(
            session,
            action="workspace_member_remove",
            resource_type="workspace_membership",
            resource_id=f"{workspace_id}:{user_id}",
            request_id=principal.request_id,
            actor_user_id=self._user_id(principal),
            organization_id=access.workspace.organization_id,
            workspace_id=workspace_id,
            safe_metadata={"outcome": "success"},
        )
        await session.commit()

    async def change_organization_member_role(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        organization_id: UUID,
        target_user_id: UUID,
        new_role: str,
    ) -> OrganizationMembership:
        if new_role not in set(OrganizationRole):
            raise AgentHubError("INVALID_ORGANIZATION_ROLE", "Organization role is invalid.", 422)
        memberships = await self._lock_organization_memberships(session, organization_id, principal)
        target = next((item for item in memberships if item.user_id == target_user_id), None)
        ensure_not_last_owner(
            sum(item.role == OrganizationRole.OWNER for item in memberships),
            target.role if target else "",
            new_role,
        )
        if target is None:
            raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)
        target.role = new_role
        append_audit(
            session,
            action="organization_member_update",
            resource_type="organization_membership",
            resource_id=f"{organization_id}:{target_user_id}",
            request_id=principal.request_id,
            actor_user_id=self._user_id(principal),
            organization_id=organization_id,
            safe_metadata={"role": new_role},
        )
        await session.commit()
        return target

    async def remove_organization_member(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        organization_id: UUID,
        target_user_id: UUID,
    ) -> None:
        memberships = await self._lock_organization_memberships(session, organization_id, principal)
        target = next((item for item in memberships if item.user_id == target_user_id), None)
        ensure_not_last_owner(
            sum(item.role == OrganizationRole.OWNER for item in memberships),
            target.role if target else "",
            None,
        )
        if target is None:
            raise AgentHubError("RESOURCE_NOT_FOUND", "Resource was not found.", 404)
        await session.delete(target)
        append_audit(
            session,
            action="organization_member_remove",
            resource_type="organization_membership",
            resource_id=f"{organization_id}:{target_user_id}",
            request_id=principal.request_id,
            actor_user_id=self._user_id(principal),
            organization_id=organization_id,
            safe_metadata={"outcome": "success"},
        )
        await session.commit()

    async def _require_workspace_admin(
        self,
        session: AsyncSession,
        principal: PrincipalContext,
        workspace_id: UUID,
        operation: str,
    ) -> WorkspaceAccess:
        access = await self.get_workspace_access(
            session, principal=principal, workspace_id=workspace_id
        )
        if not has_permission(access.context.permissions, WORKSPACE_ADMIN):
            await self._deny(
                session,
                principal=principal,
                resource_type="workspace",
                resource_id=str(workspace_id),
                organization_id=access.workspace.organization_id,
                workspace_id=workspace_id,
                operation=operation,
                status_code=403,
            )
        return access

    async def _lock_organization_memberships(
        self,
        session: AsyncSession,
        organization_id: UUID,
        principal: PrincipalContext,
    ) -> list[OrganizationMembership]:
        tenant = SqlAlchemyTenantRepository(session)
        actor_membership = await tenant.get_organization_membership(
            organization_id, self._user_id(principal)
        )
        if actor_membership is None or actor_membership.role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            await self._deny(
                session,
                principal=principal,
                resource_type="organization",
                resource_id=str(organization_id),
                organization_id=organization_id,
                operation="organization_member_mutation",
                status_code=404,
            )
        return await tenant.get_organization_memberships_for_update(organization_id)

    async def _deny(
        self,
        session: AsyncSession,
        *,
        principal: PrincipalContext,
        resource_type: str,
        resource_id: str,
        organization_id: UUID | None,
        operation: str,
        status_code: int,
        workspace_id: UUID | None = None,
    ) -> None:
        append_audit(
            session,
            action="authorization_denied",
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=principal.request_id,
            actor_user_id=self._user_id(principal),
            organization_id=organization_id,
            workspace_id=workspace_id,
            safe_metadata={"operation": operation},
        )
        await session.commit()
        code = "RESOURCE_NOT_FOUND" if status_code == 404 else "FORBIDDEN"
        message = "Resource was not found." if status_code == 404 else "You do not have permission."
        raise AgentHubError(code, message, status_code)

    @staticmethod
    def _user_id(principal: PrincipalContext) -> UUID:
        if principal.user_id is None:
            raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401)
        return UUID(str(principal.user_id))
