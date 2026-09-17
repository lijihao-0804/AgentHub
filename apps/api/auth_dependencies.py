from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from packages.control_plane.repositories import SqlAlchemyUserRepository
from packages.core.auth.security import ExpiredAccessToken, InvalidAccessToken, decode_access_token
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext

db_session_dependency = Depends(get_db_session)


async def get_current_principal(
    request: Request,
    session: AsyncSession = db_session_dependency,
) -> PrincipalContext:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401)

    settings: Settings = request.app.state.settings
    try:
        claims = decode_access_token(token, settings)
        user_id = UUID(str(claims["sub"]))
    except ExpiredAccessToken as exc:
        raise AgentHubError("ACCESS_TOKEN_EXPIRED", "Access token has expired.", 401) from exc
    except (InvalidAccessToken, ValueError) as exc:
        raise AgentHubError("INVALID_ACCESS_TOKEN", "Access token is invalid.", 401) from exc

    user = await SqlAlchemyUserRepository(session).get_by_id(user_id)
    if user is None or not user.is_active:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401)
    return PrincipalContext(
        request_id=request.state.request_id,
        trace_id=getattr(request.state, "trace_id", request.state.request_id),
        user_id=str(user.id),
    )
