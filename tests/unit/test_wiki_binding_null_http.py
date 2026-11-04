from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

import apps.api.routes.product_control_plane as product_control_plane_route
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context

_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
_AGENT_ID = UUID("00000000-0000-0000-0000-000000000002")
_KNOWLEDGE_BASE_ID = UUID("00000000-0000-0000-0000-000000000003")
_PUBLICATION_ID = UUID("00000000-0000-0000-0000-000000000004")
_PATH = f"/api/v1/workspaces/{_WORKSPACE_ID}/agents/{_AGENT_ID}/wiki-binding"


class _Service:
    def __init__(self) -> None:
        self.payloads: list[dict | None] = []

    async def replace_wiki_binding(self, _session, _context, agent_id, payload):
        assert agent_id == _AGENT_ID
        self.payloads.append(payload)
        return None


async def _request(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes | None = None,
) -> tuple[httpx.Response, _Service]:
    fake_service = _Service()
    monkeypatch.setattr(product_control_plane_route, "service", fake_service)
    app = FastAPI()
    app.include_router(product_control_plane_route.router)

    async def session_override() -> AsyncIterator[object]:
        yield object()

    async def context_override():
        return SimpleNamespace(
            workspace_id=str(_WORKSPACE_ID),
            permissions=frozenset({"agent_edit"}),
        )

    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_workspace_context] = context_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if body is None:
            response = await client.put(_PATH)
        else:
            response = await client.put(
                _PATH,
                content=body,
                headers={"Content-Type": "application/json"},
            )
    return response, fake_service


@pytest.mark.asyncio
async def test_json_null_clears_wiki_binding(monkeypatch) -> None:
    response, fake_service = await _request(monkeypatch, body=b"null")

    assert response.status_code == 200, response.text
    assert fake_service.payloads == [None]


@pytest.mark.asyncio
async def test_omitted_body_also_clears_wiki_binding(monkeypatch) -> None:
    response, fake_service = await _request(monkeypatch)

    assert response.status_code == 200, response.text
    assert fake_service.payloads == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "knowledge_base_id": str(_KNOWLEDGE_BASE_ID),
            "binding_mode": "PINNED",
            "wiki_publication_id": str(_PUBLICATION_ID),
        },
        {
            "knowledge_base_id": str(_KNOWLEDGE_BASE_ID),
            "binding_mode": "LATEST",
            "wiki_publication_id": None,
        },
    ],
)
async def test_object_bindings_reach_service_unchanged(monkeypatch, payload) -> None:
    response, fake_service = await _request(
        monkeypatch,
        body=json.dumps(payload).encode(),
    )

    assert response.status_code == 200, response.text
    assert fake_service.payloads == [
        {
            **payload,
            "knowledge_base_id": _KNOWLEDGE_BASE_ID,
            "wiki_publication_id": (
                _PUBLICATION_ID if payload["wiki_publication_id"] is not None else None
            ),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{}",
        b'{"binding_mode":"LATEST"}',
        b'{"knowledge_base_id":"00000000-0000-0000-0000-000000000003","binding_mode":"NONE"}',
        b'{"knowledge_base_id":"00000000-0000-0000-0000-000000000003","binding_mode":"LATEST","extra":true}',
        b"{",
    ],
)
async def test_invalid_wiki_binding_shapes_are_rejected(monkeypatch, body) -> None:
    response, fake_service = await _request(monkeypatch, body=body)

    assert response.status_code == 422
    assert fake_service.payloads == []
