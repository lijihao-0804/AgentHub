from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from packages.core.config.settings import Settings


class InvalidAccessToken(ValueError):
    """Raised when an access token is invalid or contains unsupported claims."""


class ExpiredAccessToken(InvalidAccessToken):
    """Raised when an access token has expired."""


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hasher.hash("agenthub-invalid-login")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        candidate_hash = password_hash or self._dummy_hash
        try:
            return self._hasher.verify(candidate_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_access_token(
    user_id: UUID,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "jti": str(uuid4()),
        "iat": int(issued_at.timestamp()),
        "exp": int(
            (issued_at + timedelta(seconds=settings.auth_access_token_ttl_seconds)).timestamp()
        ),
    }
    return jwt.encode(claims, settings.auth_jwt_secret, algorithm="HS256")


def decode_access_token(token: str, settings: Settings) -> dict[str, object]:
    try:
        claims = jwt.decode(
            token,
            settings.auth_jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "jti", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredAccessToken("access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidAccessToken("invalid access token") from exc

    if set(claims) != {"sub", "jti", "iat", "exp"}:
        raise InvalidAccessToken("unsupported access token claims")
    if not isinstance(claims["sub"], str) or not isinstance(claims["jti"], str):
        raise InvalidAccessToken("invalid access token identity claims")
    try:
        UUID(claims["sub"])
    except ValueError as exc:
        raise InvalidAccessToken("invalid access token subject") from exc
    return claims
