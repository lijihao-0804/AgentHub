import httpx
import pytest
from fastapi import Request

from apps.api.app import create_app
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError


@pytest.mark.asyncio
async def test_error_envelope_contains_request_id() -> None:
    app = create_app(Settings(testing=True))

    @app.get("/api/v1/test-error")
    async def test_error(_: Request):
        raise AgentHubError("EXAMPLE_ERROR", "Example failure.", 409)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/test-error", headers={"X-Request-ID": "req-error"})

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "EXAMPLE_ERROR",
            "message": "Example failure.",
            "request_id": "req-error",
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("get", "/api/v1/not-a-route", 404, "NOT_FOUND"),
        ("post", "/api/v1/health", 405, "METHOD_NOT_ALLOWED"),
    ],
)
async def test_http_errors_use_agenthub_envelope(
    method: str, path: str, status: int, code: str
) -> None:
    app = create_app(Settings(testing=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await getattr(client, method)(path, headers={"X-Request-ID": "req-http"})

    assert response.status_code == status
    assert response.json() == {
        "error": {
            "code": code,
            "message": response.json()["error"]["message"],
            "request_id": "req-http",
        }
    }
    assert "detail" not in response.json()
