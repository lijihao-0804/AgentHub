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

    async def create(self, *, email: str, normalized_email: str, password_hash: str) -> User: ...


class AuthSessionRepository(Protocol):
    async def get_for_refresh_update(self, token_hash: str) -> AuthSession | None: ...

    async def revoke_family(self, family_id: UUID, revoked_at: datetime) -> None: ...


class TenantRepository(Protocol):
    async def get_organization_membership(
        self, organization_id: UUID, user_id: UUID
    ) -> OrganizationMembership | None: ...

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None: ...

    async def get_workspace_membership(
        self, workspace_id: UUID, user_id: UUID
    ) -> WorkspaceMembership | None: ...

    async def create_organization(self, name: str, created_by: UUID) -> Organization: ...

    async def create_workspace(self, organization_id: UUID, name: str) -> Workspace: ...
