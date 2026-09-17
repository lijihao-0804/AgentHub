from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from packages.control_plane.models import (
    AuthSession,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)


class UserRepository(Protocol):
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def get_by_normalized_email(self, normalized_email: str) -> User | None: ...

    def add(self, user: User) -> User: ...


class AuthSessionRepository(Protocol):
    async def get_for_refresh_update(self, token_hash: str) -> AuthSession | None: ...

    async def revoke_family(self, family_id: UUID, revoked_at: datetime) -> None: ...


class TenantRepository(Protocol):
    async def get_organization_membership(
        self, organization_id: UUID, user_id: UUID, *, for_update: bool = False
    ) -> OrganizationMembership | None: ...

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None: ...

    async def get_workspace_membership(
        self, workspace_id: UUID, user_id: UUID, *, for_update: bool = False
    ) -> WorkspaceMembership | None: ...

    async def list_organizations(self, user_id: UUID) -> list[Organization]: ...

    async def list_workspaces(self, user_id: UUID) -> list[Workspace]: ...

    async def list_workspace_members(
        self, workspace_id: UUID
    ) -> list[tuple[WorkspaceMembership, User]]: ...

    async def list_organization_members(
        self, organization_id: UUID
    ) -> list[tuple[OrganizationMembership, User]]: ...

    async def get_organization_memberships_for_update(
        self, organization_id: UUID
    ) -> list[OrganizationMembership]: ...
