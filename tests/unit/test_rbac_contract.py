from packages.core.config.settings import Settings


def test_refresh_cookie_secure_defaults_to_local_false_and_nonlocal_true() -> None:
    assert Settings(environment="local").refresh_cookie_secure is False
    assert Settings(environment="production").refresh_cookie_secure is True
    assert (
        Settings(environment="production", auth_cookie_secure=False).refresh_cookie_secure is False
    )
