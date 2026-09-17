from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.schemas.auth import AuthResponse, CredentialsRequest, LogoutResponse
from packages.core.auth.service import AuthService
from packages.core.config.settings import Settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
db_session_dependency = Depends(get_db_session)


def set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.auth_refresh_cookie_name,
        value=token,
        max_age=settings.auth_refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.auth_refresh_cookie_samesite,
        path=settings.auth_refresh_cookie_path,
    )


def delete_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.auth_refresh_cookie_name,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.auth_refresh_cookie_samesite,
        path=settings.auth_refresh_cookie_path,
    )


def to_response(result) -> AuthResponse:
    return AuthResponse(
        user_id=str(result.user_id),
        access_token=result.access_token,
        expires_in=result.expires_in,
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    payload: CredentialsRequest,
    request: Request,
    response: Response,
    session: AsyncSession = db_session_dependency,
) -> AuthResponse:
    settings: Settings = request.app.state.settings
    result = await AuthService(settings).register(
        session,
        email=payload.email,
        password=payload.password,
        request_id=request.state.request_id,
    )
    set_refresh_cookie(response, result.refresh_token, settings)
    return to_response(result)


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: CredentialsRequest,
    request: Request,
    response: Response,
    session: AsyncSession = db_session_dependency,
) -> AuthResponse:
    settings: Settings = request.app.state.settings
    result = await AuthService(settings).login(
        session,
        email=payload.email,
        password=payload.password,
        request_id=request.state.request_id,
    )
    set_refresh_cookie(response, result.refresh_token, settings)
    return to_response(result)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = db_session_dependency,
) -> AuthResponse:
    settings: Settings = request.app.state.settings
    refresh_token = request.cookies.get(settings.auth_refresh_cookie_name)
    if not refresh_token:
        from packages.core.errors.exceptions import AgentHubError

        raise AgentHubError("INVALID_REFRESH_TOKEN", "Refresh session is invalid.", 401)
    result = await AuthService(settings).refresh(
        session,
        refresh_token=refresh_token,
        request_id=request.state.request_id,
    )
    set_refresh_cookie(response, result.refresh_token, settings)
    return to_response(result)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = db_session_dependency,
) -> LogoutResponse:
    settings: Settings = request.app.state.settings
    await AuthService(settings).logout(
        session,
        refresh_token=request.cookies.get(settings.auth_refresh_cookie_name),
        request_id=request.state.request_id,
    )
    delete_refresh_cookie(response, settings)
    return LogoutResponse()
