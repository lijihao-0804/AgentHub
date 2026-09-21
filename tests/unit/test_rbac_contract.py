from tests.support.settings import declared_settings


def test_refresh_cookie_secure_defaults_to_local_false_and_nonlocal_true() -> None:
    assert declared_settings(environment="local").refresh_cookie_secure is False
    assert (
        declared_settings(environment="production", auth_jwt_secret="x" * 32).refresh_cookie_secure
        is True
    )
    assert (
        declared_settings(
            environment="production",
            auth_jwt_secret="x" * 32,
            auth_cookie_secure=False,
        ).refresh_cookie_secure
        is False
    )
