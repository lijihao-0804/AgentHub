import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from tests.support import ae6b_database_urls
from tests.support.ae6b_database_urls import (
    UnsafeTestDatabaseURL,
    async_test_database_url,
    require_isolated_test_database_url,
    sync_test_database_url,
)


@pytest.mark.parametrize(
    "database_name",
    [
        "agenthub_test",
        "agenthub_context_policy_m2",
        "agenthub_run_budget_m4_20261028_v4",
        "ci_agent_hub",
    ],
)
def test_ae6b_database_url_accepts_explicit_isolated_databases(database_name: str) -> None:
    parsed = require_isolated_test_database_url(
        f"postgresql+asyncpg://tester:secret@db.example/{database_name}"
    )

    assert parsed.database == database_name
    assert parsed.host == "db.example"


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        "sqlite:///agenthub_test",
        "postgresql+asyncpg://tester:test@db.example/agenthub",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_dev",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_policy",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_budget",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_dev_budget",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_context_policy_m2_shadow",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_run_budget_m4_20261028_v4_copy",
        "postgresql+asyncpg://tester:secret@db.example/contest",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_prod_test",
        "postgresql+asyncpg://tester:secret@db.example/production_test",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_live_ci",
        "postgresql+asyncpg://tester:secret@db.example/agenthub_business_test",
    ],
)
def test_ae6b_database_url_rejects_business_or_non_postgres_databases(
    database_url: str,
) -> None:
    with pytest.raises(UnsafeTestDatabaseURL) as rejected:
        require_isolated_test_database_url(database_url)
    assert "secret" not in str(rejected.value)
    if database_url:
        assert database_url not in str(rejected.value)


def test_ae6b_database_url_error_does_not_chain_a_credential_bearing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_url = "postgresql+asyncpg://tester:secret@db.example/agenthub_test"

    def reject_url(_: str) -> None:
        raise ArgumentError(f"could not parse {credential_url}")

    monkeypatch.setattr(ae6b_database_urls, "make_url", reject_url)
    with pytest.raises(UnsafeTestDatabaseURL) as rejected:
        require_isolated_test_database_url(credential_url)

    assert rejected.value.__cause__ is None
    assert "secret" not in str(rejected.value)


def test_ae6b_database_url_keeps_each_execution_boundary_on_its_driver() -> None:
    source = "postgresql://tester:p%40ss@db.example:5432/agenthub_test?application_name=ae6b"

    async_url = async_test_database_url(source)
    sync_url = sync_test_database_url(source)

    assert async_url.drivername == "postgresql+asyncpg"
    assert sync_url.drivername == "postgresql+psycopg"
    assert async_url.database == sync_url.database == "agenthub_test"
    assert async_url.password == sync_url.password == "p@ss"
    assert async_url.query == sync_url.query == {"application_name": "ae6b"}
    rendered_sync_url = sync_url.render_as_string(hide_password=False)
    assert make_url(rendered_sync_url).password == "p@ss"
    assert make_url(source).drivername == "postgresql"
