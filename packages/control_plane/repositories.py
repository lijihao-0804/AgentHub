from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.models import AuthSession, User


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
