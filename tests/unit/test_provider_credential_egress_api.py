from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.routes import product_control_plane as product_control_plane_route
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.control_plane.rbac import WORKSPACE_ADMIN
from packages.core.errors.exceptions import AgentHubError
from packages.core.errors.handlers import install_error_handlers
from packages.model_gateway.egress import ENDPOINT_INVALID
from packages.model_gateway.models import ProviderCredential

_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
_CREDENTIAL_ID = UUID("00000000-0000-0000-0000-000000000002")
_ENDPOINT = f"/api/v1/workspaces/{_WORKSPACE_ID}/provider-credentials"


class _Result:
    def __init__(self, credential: ProviderCredential) -> None:
        self._mapping = {
            "id": credential.id,
            "workspace_id": credential.workspace_id,
            "provider": credential.provider,
            "name": credential.name,
            "base_url": credential.base_url,
            "enabled": credential.enabled,
            "created_at": credential.created_at,
        }

    def one_or_none(self):
        return SimpleNamespace(_mapping=self._mapping)


class _Session:
    def __init__(self, credential: ProviderCredential | None = None) -> None:
        self.credential = credential
        self.added: list[ProviderCredential] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, credential: ProviderCredential) -> None:
        if credential.id is None:
            credential.id = uuid4()
        if credential.created_at is None:
            credential.created_at = datetime.now(UTC)
        self.credential = credential
        self.added.append(credential)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def scalar(self, _statement):
        return self.credential

    async def execute(self, _statement):
        assert self.credential is not None
        return _Result(self.credential)


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id=str(_WORKSPACE_ID),
        permissions=frozenset({WORKSPACE_ADMIN, "workspace_read"}),
    )


def _credential(*, base_url: str | None = "https://provider.example/v1") -> ProviderCredential:
    return ProviderCredential(
        id=_CREDENTIAL_ID,
        workspace_id=_WORKSPACE_ID,
        provider="openai-compatible",
        name="provider",
        secret="test-secret",
        base_url=base_url,
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def _http_client(
    monkeypatch: pytest.MonkeyPatch,
    session: _Session,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(
        product_control_plane_route,
        "service",
        ProductControlPlaneService(),
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(product_control_plane_route.router)

    async def session_override():
        yield session

    async def context_override():
        return _context()

    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_workspace_context] = context_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_create_service_normalizes_regional_and_path_shape() -> None:
    service = ProductControlPlaneService()
    session = _Session()

    item = await service.create_provider_credential(
        session,
        _context(),
        provider="openai-compatible",
        name="provider",
        secret="test-secret",
        base_url="HTTPS://Provider.Example.:443/v1/",
        enabled=True,
    )

    assert item["base_url"] == "https://provider.example/v1"
    assert session.credential is not None
    assert session.credential.base_url == "https://provider.example/v1"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_create_service_preserves_none_as_sdk_default() -> None:
    service = ProductControlPlaneService()
    session = _Session()

    item = await service.create_provider_credential(
        session,
        _context(),
        provider="deepseek",
        name="default endpoint",
        secret="test-secret",
        base_url=None,
        enabled=True,
    )

    assert item["base_url"] is None
    assert session.credential is not None
    assert session.credential.base_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:top-secret@provider.example/v1",
        "https://provider.example/v1?token=top-secret",
        "http://10.0.0.1/v1",
        "ftp://provider.example/v1",
    ],
)
async def test_create_service_rejects_structural_egress_violations(base_url: str) -> None:
    service = ProductControlPlaneService()
    session = _Session()

    with pytest.raises(AgentHubError) as raised:
        await service.create_provider_credential(
            session,
            _context(),
            provider="openai-compatible",
            name="provider",
            secret="test-secret",
            base_url=base_url,
            enabled=True,
        )

    assert raised.value.code == ENDPOINT_INVALID
    assert raised.value.status_code == 422
    assert raised.value.message == "The provider base URL is invalid."
    assert base_url not in raised.value.message
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_patch_without_base_url_does_not_clear_it() -> None:
    service = ProductControlPlaneService()
    credential = _credential(base_url="https://provider.example/v1")
    session = _Session(credential)

    item = await service.patch_provider_credential(
        session,
        _context(),
        _CREDENTIAL_ID,
        {"name": "renamed"},
    )

    assert item["base_url"] == "https://provider.example/v1"
    assert credential.base_url == "https://provider.example/v1"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_patch_explicit_null_clears_to_sdk_default() -> None:
    service = ProductControlPlaneService()
    credential = _credential(base_url="https://provider.example/v1")
    session = _Session(credential)

    item = await service.patch_provider_credential(
        session,
        _context(),
        _CREDENTIAL_ID,
        {"base_url": None},
    )

    assert item["base_url"] is None
    assert credential.base_url is None


@pytest.mark.asyncio
async def test_patch_invalid_url_does_not_partially_mutate_credential() -> None:
    service = ProductControlPlaneService()
    credential = _credential()
    session = _Session(credential)

    with pytest.raises(AgentHubError) as raised:
        await service.patch_provider_credential(
            session,
            _context(),
            _CREDENTIAL_ID,
            {"name": "changed", "base_url": "https://provider.example/v1#fragment"},
        )

    assert raised.value.code == ENDPOINT_INVALID
    assert credential.name == "provider"
    assert credential.base_url == "https://provider.example/v1"
    assert session.commits == 0


@pytest.mark.asyncio
async def test_http_create_returns_normalized_credential(monkeypatch) -> None:
    session = _Session()
    async for client in _http_client(monkeypatch, session):
        response = await client.post(
            _ENDPOINT,
            json={
                "provider": "openai-compatible",
                "name": "provider",
                "secret": "test-secret",
                "base_url": "HTTPS://Provider.Example.:443/v1/",
                "enabled": True,
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["base_url"] == "https://provider.example/v1"
    assert "test-secret" not in response.text


@pytest.mark.asyncio
async def test_http_create_null_keeps_api_default_intent(monkeypatch) -> None:
    session = _Session()
    async for client in _http_client(monkeypatch, session):
        response = await client.post(
            _ENDPOINT,
            json={
                "provider": "deepseek",
                "name": "default endpoint",
                "secret": "test-secret",
                "base_url": None,
                "enabled": True,
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["base_url"] is None


@pytest.mark.asyncio
async def test_http_patch_omitting_base_url_preserves_it(monkeypatch) -> None:
    credential = _credential()
    session = _Session(credential)
    async for client in _http_client(monkeypatch, session):
        response = await client.patch(
            f"{_ENDPOINT}/{_CREDENTIAL_ID}",
            json={"name": "renamed"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["base_url"] == "https://provider.example/v1"
    assert credential.base_url == "https://provider.example/v1"


@pytest.mark.asyncio
async def test_http_patch_explicit_null_clears_default_intent(monkeypatch) -> None:
    credential = _credential()
    session = _Session(credential)
    async for client in _http_client(monkeypatch, session):
        response = await client.patch(
            f"{_ENDPOINT}/{_CREDENTIAL_ID}",
            json={"base_url": None},
        )

    assert response.status_code == 200, response.text
    assert response.json()["base_url"] is None
    assert credential.base_url is None


@pytest.mark.asyncio
async def test_http_patch_invalid_url_uses_safe_error_envelope(monkeypatch) -> None:
    credential = _credential()
    session = _Session(credential)
    async for client in _http_client(monkeypatch, session):
        response = await client.patch(
            f"{_ENDPOINT}/{_CREDENTIAL_ID}",
            json={"base_url": "https://provider.example.to-be-hidden/v1?token=top-secret"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"] == {
        "code": ENDPOINT_INVALID,
        "message": "The provider base URL is invalid.",
        "request_id": "unknown",
    }
    assert "provider.example" not in response.text
    assert "top-secret" not in response.text
    assert credential.base_url == "https://provider.example/v1"
    assert session.commits == 0
