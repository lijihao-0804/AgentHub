import json
import logging

import httpx
import pytest

from apps.api.app import create_app
from packages.core.config.settings import Settings
from packages.core.http.request_id import RequestIdMiddleware
from packages.core.logging.json_logging import (
    JsonFormatter,
    request_id_var,
    trace_id_var,
)


@pytest.mark.asyncio
async def test_request_id_header_uses_settings_and_rejects_invalid_ids() -> None:
    seen: dict[str, str] = {}

    async def downstream(scope, receive, send) -> None:
        del receive
        seen.update(scope["state"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = RequestIdMiddleware(downstream, request_id_header="X-Correlation-ID")
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/",
            headers={"X-Correlation-ID": "request-123", "X-Trace-ID": "trace-123"},
        )

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "request-123"
    assert seen == {
        "request_id": "request-123",
        "trace_id": "trace-123",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/",
            headers={"X-Correlation-ID": "x" * 129, "X-Trace-ID": "bad value"},
        )

    assert response.status_code == 200
    assert len(response.headers["x-correlation-id"]) == 36
    assert seen["request_id"] == response.headers["x-correlation-id"]
    assert seen["trace_id"] == seen["request_id"]

    configured_app = create_app(Settings(testing=True, request_id_header="X-Correlation-ID"))
    configured_transport = httpx.ASGITransport(app=configured_app)
    async with httpx.AsyncClient(
        transport=configured_transport, base_url="http://test"
    ) as client:
        configured_response = await client.get(
            "/api/v1/health", headers={"X-Correlation-ID": "configured-request"}
        )
    assert configured_response.headers["x-correlation-id"] == "configured-request"


def test_json_formatter_adds_context_and_omits_empty_context() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)

    empty_payload = json.loads(formatter.format(record))
    assert "request_id" not in empty_payload
    assert "trace_id" not in empty_payload

    request_token = request_id_var.set("request-1")
    trace_token = trace_id_var.set("trace-1")
    try:
        payload = json.loads(formatter.format(record))
    finally:
        trace_id_var.reset(trace_token)
        request_id_var.reset(request_token)

    assert payload["request_id"] == "request-1"
    assert payload["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_request_context_is_reset_after_request() -> None:
    app = create_app(Settings(testing=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health", headers={"X-Request-ID": "request-1"})

    assert response.status_code == 200
    assert request_id_var.get() is None
    assert trace_id_var.get() is None
