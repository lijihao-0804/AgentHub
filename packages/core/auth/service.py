from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.audit import append_audit
from packages.control_plane.models import AuthSession, User
from packages.control_plane.repositories import (
    SqlAlchemyAuthSessionRepository,
    SqlAlchemyUserRepository,
)
from packages.control_plane.repository_contracts import AuthSessionRepository, UserRepository
from packages.core.auth.security import (
    PasswordService,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
    normalize_email,
)
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError

INVALID_CREDENTIALS = "Invalid email or password."


@dataclass(frozen=True)
class AuthenticationResult:
    user_id: UUID
    access_token: str
    refresh_token: str
    expires_in: int


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.passwords = PasswordService()

    async def register(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        request_id: str,
    ) -> AuthenticationResult:
        users: UserRepository = SqlAlchemyUserRepository(session)
        normalized_email = normalize_email(email)
        if await users.get_by_normalized_email(normalized_email) is not None:
            raise AgentHubError("EMAIL_ALREADY_REGISTERED", "Email is already registered.", 409)

        user = users.add(
            User(
                email=email.strip(),
                normalized_email=normalized_email,
                password_hash=self.passwords.hash(password),
            )
        )
        try:
            await session.flush()
            result = await self._create_session(
                session, user, request_id=request_id, action="register"
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "EMAIL_ALREADY_REGISTERED", "Email is already registered.", 409
            ) from exc
        return result

    async def login(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        request_id: str,
    ) -> AuthenticationResult:
        users: UserRepository = SqlAlchemyUserRepository(session)
        user = await users.get_by_normalized_email(normalize_email(email))
        valid_password = self.passwords.verify(user.password_hash if user else None, password)
        if user is None or not user.is_active or not valid_password:
            append_audit(
                session,
                action="login_failure",
                resource_type="user",
                resource_id=str(user.id) if user else None,
                request_id=request_id,
                actor_user_id=None,
                safe_metadata={"reason": "invalid_credentials"},
            )
            await session.commit()
            raise AgentHubError("INVALID_CREDENTIALS", INVALID_CREDENTIALS, 401)

        result = await self._create_session(
            session, user, request_id=request_id, action="login_success"
        )
        await session.commit()
        return result

    async def refresh(
        self,
        session: AsyncSession,
        *,
        refresh_token: str,
        request_id: str,
    ) -> AuthenticationResult:
        sessions: AuthSessionRepository = SqlAlchemyAuthSessionRepository(session)
        current = await sessions.get_for_refresh_update(hash_refresh_token(refresh_token))
        if current is None:
            raise AgentHubError("INVALID_REFRESH_TOKEN", "Refresh session is invalid.", 401)

        now = datetime.now(UTC)
        user = await SqlAlchemyUserRepository(session).get_by_id(current.user_id)
        if user is None or not user.is_active:
            await sessions.revoke_family(current.family_id, now)
            append_audit(
                session,
                action="refresh_invalid_user",
                resource_type="auth_session",
                resource_id=str(current.id),
                request_id=request_id,
                actor_user_id=current.user_id,
                safe_metadata={"outcome": "family_revoked"},
            )
            await session.commit()
            raise AgentHubError("INVALID_REFRESH_TOKEN", "Refresh session is invalid.", 401)
        if current.used_at is not None or current.rotated_at is not None:
            await sessions.revoke_family(current.family_id, now)
            append_audit(
                session,
                action="refresh_reuse",
                resource_type="auth_session",
                resource_id=str(current.id),
                request_id=request_id,
                actor_user_id=current.user_id,
                safe_metadata={"outcome": "family_revoked"},
            )
            await session.commit()
            raise AgentHubError("REFRESH_TOKEN_REUSE", "Refresh session reuse detected.", 401)
        if current.revoked_at is not None:
            raise AgentHubError("REFRESH_TOKEN_REVOKED", "Refresh session is revoked.", 401)
        if current.expires_at <= now:
            raise AgentHubError("REFRESH_TOKEN_EXPIRED", "Refresh session has expired.", 401)

        current.used_at = now
        current.rotated_at = now
        raw_next = new_refresh_token()
        replacement = AuthSession(
            user_id=current.user_id,
            family_id=current.family_id,
            token_hash=hash_refresh_token(raw_next),
            expires_at=now + timedelta(seconds=self.settings.auth_refresh_token_ttl_seconds),
        )
        session.add(replacement)
        await session.flush()
        current.replacement_session_id = replacement.id
        append_audit(
            session,
            action="refresh",
            resource_type="auth_session",
            resource_id=str(replacement.id),
            request_id=request_id,
            actor_user_id=current.user_id,
            safe_metadata={"outcome": "rotated"},
        )
        await session.commit()
        return AuthenticationResult(
            user_id=current.user_id,
            access_token=issue_access_token(current.user_id, self.settings, now),
            refresh_token=raw_next,
            expires_in=self.settings.auth_access_token_ttl_seconds,
        )

    async def logout(
        self,
        session: AsyncSession,
        *,
        refresh_token: str | None,
        request_id: str,
    ) -> None:
        if not refresh_token:
            return
        sessions: AuthSessionRepository = SqlAlchemyAuthSessionRepository(session)
        current = await sessions.get_for_refresh_update(hash_refresh_token(refresh_token))
        if current is None:
            return
        now = datetime.now(UTC)
        await sessions.revoke_family(current.family_id, now)
        append_audit(
            session,
            action="logout",
            resource_type="auth_session",
            resource_id=str(current.id),
            request_id=request_id,
            actor_user_id=current.user_id,
            safe_metadata={"outcome": "family_revoked"},
        )
        await session.commit()

    async def _create_session(
        self,
        session: AsyncSession,
        user: User,
        *,
        request_id: str,
        action: str,
    ) -> AuthenticationResult:
        now = datetime.now(UTC)
        raw_refresh = new_refresh_token()
        auth_session = AuthSession(
            user_id=user.id,
            family_id=uuid4(),
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=now + timedelta(seconds=self.settings.auth_refresh_token_ttl_seconds),
        )
        session.add(auth_session)
        await session.flush()
        append_audit(
            session,
            action=action,
            resource_type="user",
            resource_id=str(user.id),
            request_id=request_id,
            actor_user_id=user.id,
            safe_metadata={"outcome": "success"},
        )
        return AuthenticationResult(
            user_id=user.id,
            access_token=issue_access_token(user.id, self.settings, now),
            refresh_token=raw_refresh,
            expires_in=self.settings.auth_access_token_ttl_seconds,
        )
