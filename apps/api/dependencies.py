from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.errors.exceptions import AgentHubError


def get_db_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise AgentHubError(
            "DATABASE_NOT_CONFIGURED",
            "Database access is not configured.",
            status_code=503,
        )
    return factory


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = get_db_session_factory(request)
    async with factory() as session:
        yield session
