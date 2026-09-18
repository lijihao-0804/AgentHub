from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from packages.core.auth.security import (
    ExpiredAccessToken,
    InvalidAccessToken,
    PasswordService,
    decode_access_token,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
    normalize_email,
)
from packages.core.config.settings import DEFAULT_AUTH_JWT_SECRET, Settings


def test_passwords_use_argon2id_and_verify() -> None:
    service = PasswordService()
    password_hash = service.hash("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")
    assert service.verify(password_hash, "correct horse battery staple")
    assert not service.verify(password_hash, "wrong password")
    assert not service.verify(None, "wrong password")


def test_refresh_tokens_are_opaque_and_hashed() -> None:
    token = new_refresh_token()

    assert len(token) >= 80
    assert token != new_refresh_token()
    assert len(hash_refresh_token(token)) == 64
    assert hash_refresh_token(token) != token


def test_access_token_contains_only_identity_claims() -> None:
    user_id = uuid4()
    settings = Settings(testing=True)
    now = datetime.now(UTC).replace(microsecond=0)

    token = issue_access_token(user_id, settings, now)
    claims = decode_access_token(token, settings)

    assert set(claims) == {"sub", "jti", "iat", "exp"}
    assert claims["sub"] == str(user_id)


def test_access_token_rejects_extra_claims_and_expired_tokens() -> None:
    settings = Settings(testing=True)
    user_id = str(uuid4())
    now = datetime.now(UTC).replace(microsecond=0)
    extra_claim_token = jwt.encode(
        {
            "sub": user_id,
            "jti": str(uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "role": "ADMIN",
        },
        settings.auth_jwt_secret,
        algorithm="HS256",
    )
    expired_token = issue_access_token(
        user_id=uuid4(), settings=settings, now=now - timedelta(hours=2)
    )

    with pytest.raises(InvalidAccessToken):
        decode_access_token(extra_claim_token, settings)
    with pytest.raises(ExpiredAccessToken):
        decode_access_token(expired_token, settings)


def test_email_normalization_is_stable() -> None:
    assert normalize_email("  Alice@Example.COM ") == "alice@example.com"


@pytest.mark.parametrize("environment", ["local", "test", "development"])
def test_default_jwt_secret_is_allowed_only_for_dev_environments(environment: str) -> None:
    assert Settings(environment=environment).auth_jwt_secret == DEFAULT_AUTH_JWT_SECRET


@pytest.mark.parametrize("environment", ["docker", "staging", "production"])
def test_default_jwt_secret_fails_fast_outside_dev_environments(environment: str) -> None:
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        Settings(environment=environment)


def test_custom_production_jwt_secret_is_accepted() -> None:
    settings = Settings(environment="production", auth_jwt_secret="x" * 32)
    assert settings.auth_jwt_secret == "x" * 32
