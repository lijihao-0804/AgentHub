from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.enums import OrganizationRole
from packages.control_plane.models import (
    AuthSession,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_normalized_email(self, normalized_email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.normalized_email == normalized_email)
        )
        return result.scalar_one_or_none()

    def add(self, user: User) -> User:
        self.session.add(user)
        return user


class SqlAlchemyAuthSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_refresh_update(self, token_hash: str) -> AuthSession | None:
        result = await self.session.execute(
            select(AuthSession).where(AuthSession.token_hash == token_hash).with_for_update()
        )
        return result.scalar_one_or_none()

    async def revoke_family(self, family_id: UUID, revoked_at: datetime) -> None:
        await self.session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )


class SqlAlchemyTenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_organization_membership(
        self,
        organization_id: UUID,
        user_id: UUID,
        *,
        for_update: bool = False,
    ) -> OrganizationMembership | None:
        statement = select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        return await self.session.get(Workspace, workspace_id)

    async def get_workspace_membership(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkspaceMembership | None:
        statement = select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_organizations(self, user_id: UUID) -> list[Organization]:
        result = await self.session.execute(
            select(Organization)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Organization.id,
            )
            .where(OrganizationMembership.user_id == user_id)
            .order_by(Organization.created_at, Organization.id)
        )
        return list(result.scalars().all())

    async def list_workspaces(self, user_id: UUID) -> list[Workspace]:
        workspace_membership = select(WorkspaceMembership.workspace_id).where(
            WorkspaceMembership.workspace_id == Workspace.id,
            WorkspaceMembership.user_id == user_id,
        )
        result = await self.session.execute(
            select(Workspace)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Workspace.organization_id,
            )
            .where(
                OrganizationMembership.user_id == user_id,
                or_(
                    OrganizationMembership.role.in_([
                        OrganizationRole.OWNER,
                        OrganizationRole.ADMIN,
                    ]),
                    workspace_membership.exists(),
                ),
            )
            .order_by(Workspace.created_at, Workspace.id)
        )
        return list(result.scalars().all())

    async def list_workspace_members(
        self, workspace_id: UUID
    ) -> list[tuple[WorkspaceMembership, User]]:
        result = await self.session.execute(
            select(WorkspaceMembership, User)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(WorkspaceMembership.workspace_id == workspace_id)
            .order_by(User.normalized_email)
        )
        return list(result.all())

    async def list_organization_members(
        self, organization_id: UUID
    ) -> list[tuple[OrganizationMembership, User]]:
        result = await self.session.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(OrganizationMembership.organization_id == organization_id)
            .order_by(User.normalized_email)
        )
        return list(result.all())

    async def get_organization_memberships_for_update(
        self, organization_id: UUID
    ) -> list[OrganizationMembership]:
        result = await self.session.execute(
            select(OrganizationMembership)
            .where(OrganizationMembership.organization_id == organization_id)
            .with_for_update()
        )
        return list(result.scalars().all())
