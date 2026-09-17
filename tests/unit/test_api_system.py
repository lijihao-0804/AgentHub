import httpx
import pytest

from apps.api.app import create_app
from packages.core.config.settings import Settings


@pytest.mark.asyncio
async def test_health_is_live_and_returns_request_id() -> None:
    app = create_app(Settings(testing=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["request_id"] == "test-request"
    assert response.headers["x-request-id"] == "test-request"


@pytest.mark.asyncio
async def test_ready_only_requires_postgres() -> None:
    app = create_app(Settings(testing=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["dependencies"]["postgres"] == "degraded"
