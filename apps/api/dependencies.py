from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.errors.exceptions import AgentHubError


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise AgentHubError(
            "DATABASE_NOT_CONFIGURED",
            "Database access is not configured.",
            status_code=503,
        )
    async with factory() as session:
        yield session
