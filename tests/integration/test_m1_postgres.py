from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.app import create_app
from packages.control_plane.models import (
    AuditLog,
    AuthSession,
    OrganizationMembership,
    User,
    WorkspaceMembership,
)
from packages.control_plane.services import TenantService
from packages.core.auth.security import hash_refresh_token, issue_access_token
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
COOKIE_NAME = "agenthub_refresh"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M1 PostgreSQL integration tests.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        config = Config(str(Path("alembic.ini")))
        command.upgrade(config, "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture(scope="session")
async def db_resources(
    migrated_database: None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app(db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]):
    engine, factory = db_resources
    settings = Settings(
        testing=False,
        environment="local",
        database_url=async_database_url(TEST_DATABASE_URL),
    )
    application = create_app(settings)
    application.state.db_engine = engine
    application.state.db_session_factory = factory
    yield application


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def register(
    client: httpx.AsyncClient, *, email: str | None = None
) -> tuple[UUID, str, str, str]:
    email = email or f"{uuid4()}@example.test"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    refresh_token = client.cookies.get(COOKIE_NAME)
    assert refresh_token is not None
    return UUID(payload["user_id"]), payload["access_token"], refresh_token, email


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def error_code(response: httpx.Response) -> str:
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "request_id"}
    return payload["error"]["code"]


async def set_user_active(
    factory: async_sessionmaker[AsyncSession], user_id: UUID, active: bool
) -> None:
    async with factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(is_active=active))
        await session.commit()


async def set_org_role(
    factory: async_sessionmaker[AsyncSession], organization_id: UUID, user_id: UUID, role: str
) -> None:
    async with factory() as session:
        await session.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.user_id == user_id,
            )
            .values(role=role)
        )
        await session.commit()


async def add_org_member(
    factory: async_sessionmaker[AsyncSession], organization_id: UUID, user_id: UUID, role: str
) -> None:
    async with factory() as session:
        session.add(
            OrganizationMembership(
                organization_id=organization_id,
                user_id=user_id,
                role=role,
            )
        )
        await session.commit()


async def add_workspace_member(
    factory: async_sessionmaker[AsyncSession], workspace_id: UUID, user_id: UUID, role: str
) -> None:
    async with factory() as session:
        session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=user_id, role=role))
        await session.commit()


@pytest.mark.asyncio
async def test_m1_migration_creates_postgres_schema(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, _ = db_resources
    async with engine.connect() as connection:
        assert connection.dialect.name == "postgresql"
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "0002_m1_auth_tenant_rbac"
        tables = await connection.scalars(
            text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                    'users', 'auth_sessions', 'organizations',
                    'organization_memberships', 'workspaces',
                    'workspace_memberships', 'audit_logs'
                  )
                """
            )
        )
        assert set(tables) == {
            "users",
            "auth_sessions",
            "organizations",
            "organization_memberships",
            "workspaces",
            "workspace_memberships",
            "audit_logs",
        }


@pytest.mark.asyncio
async def test_auth_rotation_reuse_and_logout_are_postgres_backed(
    client: httpx.AsyncClient,
    app,
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    user_id, _, old_refresh, _ = await register(client)

    rotated = await client.post("/api/v1/auth/refresh", headers={"X-Request-ID": "m1-refresh"})
    assert rotated.status_code == 200, rotated.text
    new_refresh = client.cookies.get(COOKIE_NAME)
    assert new_refresh is not None and new_refresh != old_refresh
    assert rotated.headers["x-request-id"] == "m1-refresh"
    set_cookie = rotated.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/api/v1/auth" in set_cookie
    assert "secure" not in set_cookie

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as reused_client:
        reused = await reused_client.post(
            "/api/v1/auth/refresh",
            cookies={COOKIE_NAME: old_refresh},
            headers={"X-Request-ID": "m1-reuse"},
        )
    assert reused.status_code == 401
    assert error_code(reused) == "REFRESH_TOKEN_REUSE"
    assert reused.headers["x-request-id"] == "m1-reuse"

    revoked = await client.post("/api/v1/auth/refresh")
    assert revoked.status_code == 401
    assert error_code(revoked) == "REFRESH_TOKEN_REVOKED"

    async with factory() as session:
        sessions = list(
            (
                await session.scalars(
                    select(AuthSession).where(AuthSession.user_id == user_id)
                )
            ).all()
        )
    assert len(sessions) == 2
    assert all(session.token_hash not in {old_refresh, new_refresh} for session in sessions)
    expected_hashes = {hash_refresh_token(old_refresh), hash_refresh_token(new_refresh)}
    assert all(session.token_hash in expected_hashes for session in sessions)

    _, _, logout_refresh, _ = await register(client)
    logged_out = await client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 200
    after_logout = await client.post(
        "/api/v1/auth/refresh", cookies={COOKIE_NAME: logout_refresh}
    )
    assert after_logout.status_code == 401
    assert error_code(after_logout) == "REFRESH_TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_concurrent_refresh_only_one_request_succeeds(
    client: httpx.AsyncClient,
    app,
) -> None:
    _, _, refresh_token, _ = await register(client)

    async def refresh_once(request_id: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
            return await value.post(
                "/api/v1/auth/refresh",
                cookies={COOKIE_NAME: refresh_token},
                headers={"X-Request-ID": request_id},
            )

    responses = await asyncio.gather(
        refresh_once("m1-concurrent-a"), refresh_once("m1-concurrent-b")
    )
    assert sorted(response.status_code for response in responses) == [200, 401]
    failed = next(response for response in responses if response.status_code == 401)
    assert error_code(failed) == "REFRESH_TOKEN_REUSE"


@pytest.mark.asyncio
async def test_auth_failures_are_uniform_and_expired_credentials_rejected(
    client: httpx.AsyncClient,
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    user_id, _, refresh_token, email = await register(client)

    duplicate = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert duplicate.status_code == 409
    assert error_code(duplicate) == "EMAIL_ALREADY_REGISTERED"

    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong password"},
    )
    unknown_email = await client.post(
        "/api/v1/auth/login",
        json={"email": "unknown@example.test", "password": "wrong password"},
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["error"]["code"] == unknown_email.json()["error"]["code"]
    assert wrong_password.json()["error"]["message"] == unknown_email.json()["error"]["message"]

    settings = Settings(testing=True)
    expired_access = issue_access_token(
        user_id, settings, datetime.now(UTC) - timedelta(hours=2)
    )
    expired_access_response = await client.get(
        "/api/v1/organizations", headers=bearer(expired_access)
    )
    assert expired_access_response.status_code == 401
    assert error_code(expired_access_response) == "ACCESS_TOKEN_EXPIRED"

    await set_user_active(factory, user_id, False)
    inactive = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert inactive.status_code == 401
    assert error_code(inactive) == "INVALID_CREDENTIALS"

    async with factory() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == hash_refresh_token(refresh_token))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    expired_refresh_response = await client.post(
        "/api/v1/auth/refresh", cookies={COOKIE_NAME: refresh_token}
    )
    assert expired_refresh_response.status_code == 401
    assert error_code(expired_refresh_response) == "REFRESH_TOKEN_EXPIRED"

    async with factory() as session:
        logs = list(
            (await session.scalars(select(AuditLog).where(AuditLog.actor_user_id == user_id))).all()
        )
    metadata = json.dumps([log.safe_metadata for log in logs]).lower()
    assert all(
        secret not in metadata
        for secret in ("password", "token", "secret", "credential")
    )


@pytest.mark.asyncio
async def test_tenant_rbac_and_owner_protection_use_database_state(
    client: httpx.AsyncClient,
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    owner_id, owner_token, _, _ = await register(client)
    admin_id, admin_token, _, _ = await register(client)
    member_id, member_token, _, _ = await register(client)

    organization_response = await client.post(
        "/api/v1/organizations",
        json={"name": f"Org {uuid4()}"},
        headers=bearer(owner_token),
    )
    assert organization_response.status_code == 201
    organization_id = UUID(organization_response.json()["id"])
    workspace_response = await client.post(
        "/api/v1/workspaces",
        json={"organization_id": str(organization_id), "name": f"Workspace {uuid4()}"},
        headers=bearer(owner_token),
    )
    assert workspace_response.status_code == 201
    workspace_id = UUID(workspace_response.json()["id"])

    guessed = await client.get(
        f"/api/v1/workspaces/{uuid4()}",
        headers={**bearer(owner_token), "X-Request-ID": "m1-guessed"},
    )
    assert guessed.status_code == 404
    assert error_code(guessed) == "RESOURCE_NOT_FOUND"
    assert guessed.headers["x-request-id"] == "m1-guessed"

    member_without_workspace = await client.get(
        f"/api/v1/workspaces/{workspace_id}", headers=bearer(member_token)
    )
    assert member_without_workspace.status_code == 404

    await add_org_member(factory, organization_id, admin_id, "ADMIN")
    admin_access = await client.get(
        f"/api/v1/workspaces/{workspace_id}", headers=bearer(admin_token)
    )
    assert admin_access.status_code == 200
    workspaces = await client.get("/api/v1/workspaces", headers=bearer(admin_token))
    assert workspace_id in {UUID(item["id"]) for item in workspaces.json()}

    await set_org_role(factory, organization_id, admin_id, "MEMBER")
    member_after_role_change = await client.get(
        f"/api/v1/workspaces/{workspace_id}", headers=bearer(admin_token)
    )
    assert member_after_role_change.status_code == 404
    await add_workspace_member(factory, workspace_id, admin_id, "DEVELOPER")
    developer_access = await client.get(
        f"/api/v1/workspaces/{workspace_id}", headers=bearer(admin_token)
    )
    assert developer_access.status_code == 200

    async with factory() as session:
        access = await TenantService().get_workspace_access(
            session,
            principal=PrincipalContext(
                request_id="m1-context",
                trace_id="m1-context",
                user_id=admin_id,
            ),
            workspace_id=workspace_id,
        )
        assert access.context.workspace_role == "DEVELOPER"
        assert "run_action" in access.context.permissions
        assert "approve_action" not in access.context.permissions

    developer_mutation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"user_id": str(member_id), "role": "VIEWER"},
        headers=bearer(admin_token),
    )
    assert developer_mutation.status_code == 403
    assert error_code(developer_mutation) == "FORBIDDEN"

    await add_org_member(factory, organization_id, member_id, "MEMBER")
    viewer_added = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"user_id": str(member_id), "role": "VIEWER"},
        headers=bearer(owner_token),
    )
    assert viewer_added.status_code == 201
    viewer_mutation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"user_id": str(admin_id), "role": "VIEWER"},
        headers=bearer(member_token),
    )
    assert viewer_mutation.status_code == 403
    assert error_code(viewer_mutation) == "FORBIDDEN"

    other_org = await client.post(
        "/api/v1/organizations",
        json={"name": f"Other {uuid4()}"},
        headers=bearer(admin_token),
    )
    assert other_org.status_code == 201
    other_org_id = UUID(other_org.json()["id"])
    other_workspace = await client.post(
        "/api/v1/workspaces",
        json={"organization_id": str(other_org_id), "name": f"Other WS {uuid4()}"},
        headers=bearer(admin_token),
    )
    assert other_workspace.status_code == 201
    cross_tenant = await client.get(
        f"/api/v1/workspaces/{other_workspace.json()['id']}",
        headers=bearer(owner_token),
    )
    assert cross_tenant.status_code == 404

    principal = PrincipalContext(request_id="m1-owner", trace_id="m1-owner", user_id=owner_id)
    async with factory() as session:
        with pytest.raises(AgentHubError, match="last organization owner") as remove_error:
            await TenantService().remove_organization_member(
                session,
                principal=principal,
                organization_id=organization_id,
                target_user_id=owner_id,
            )
        assert remove_error.value.code == "LAST_OWNER_PROTECTION"
    async with factory() as session:
        with pytest.raises(AgentHubError, match="last organization owner"):
            await TenantService().change_organization_member_role(
                session,
                principal=principal,
                organization_id=organization_id,
                target_user_id=owner_id,
                new_role="ADMIN",
            )

        logs = list(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.organization_id == organization_id,
                        AuditLog.action == "last_owner_protection",
                    )
                )
            ).all()
        )
    assert len(logs) == 2
    assert all(log.safe_metadata == {"outcome": "denied"} for log in logs)
