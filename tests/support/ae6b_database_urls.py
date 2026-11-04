"""Database URL guards and driver adapters for AE-6b integration tests."""

from __future__ import annotations

import re

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

_POSTGRES_DRIVERS = frozenset(
    {
        "postgres",
        "postgresql",
        "postgresql+asyncpg",
        "postgresql+psycopg",
    }
)
_ISOLATION_TOKENS = frozenset({"ci", "test"})
_LEGACY_ISOLATED_DATABASES = frozenset(
    {
        "agenthub_context_policy_m2",
        "agenthub_run_budget_m4_20261028_v4",
    }
)
_BUSINESS_TOKENS = frozenset({"business", "live", "prod", "production"})


class UnsafeTestDatabaseURL(ValueError):
    """Raised when a destructive test is pointed at a non-isolated database."""


def require_isolated_test_database_url(database_url: str | None) -> URL:
    """Parse an explicitly isolated PostgreSQL test database URL."""

    if not isinstance(database_url, str) or not database_url.strip():
        raise UnsafeTestDatabaseURL("An isolated PostgreSQL test database URL is required.")
    try:
        parsed = make_url(database_url.strip())
    except (ArgumentError, TypeError, ValueError):
        raise UnsafeTestDatabaseURL(
            "The configured test database URL is not a valid SQLAlchemy PostgreSQL URL."
        ) from None
    if parsed.drivername not in _POSTGRES_DRIVERS:
        raise UnsafeTestDatabaseURL("The test database URL must use a supported PostgreSQL driver.")

    database_name = (parsed.database or "").strip().lower()
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", database_name)))
    if tokens & _BUSINESS_TOKENS or (
        database_name not in _LEGACY_ISOLATED_DATABASES and not tokens & _ISOLATION_TOKENS
    ):
        raise UnsafeTestDatabaseURL(
            "The configured URL must name an explicitly isolated test database."
        )
    return parsed


def async_test_database_url(database_url: str | None) -> URL:
    """Return the same isolated database bound to SQLAlchemy's asyncpg driver."""

    return require_isolated_test_database_url(database_url).set(drivername="postgresql+asyncpg")


def sync_test_database_url(database_url: str | None) -> URL:
    """Return the same isolated database bound to SQLAlchemy's psycopg driver."""

    return require_isolated_test_database_url(database_url).set(drivername="postgresql+psycopg")
