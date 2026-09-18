from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import (
    get_model_gateway,
    get_query_workspace_context,
    get_retrieval_components,
)
from packages.core.config.settings import Settings
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.citation_qa import CitationQaService
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from tests.unit.test_knowledge_m3e_citation_qa import (
    _context,
    _FakeModelGateway,
    _FakeSession,
    _service,
)


async def _post(app, payload: dict[str, object], headers: dict[str, str] | None = None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/knowledge/query", json=payload, headers=headers or {})


def _payload(session: _FakeSession) -> dict[str, object]:
    return {
        "workspace_id": str(session.workspace_id),
        "knowledge_base_id": str(session.knowledge_base_id),
        "knowledge_snapshot_id": str(session.snapshot_id),
        "model_profile_id": str(uuid4()),
        "query": "What does the handbook say?",
    }


def _app(
    session: _FakeSession,
    gateway: _FakeModelGateway,
    context: WorkspaceExecutionContext | None = None,
):
    app = create_app(Settings(testing=True, knowledge_dense_vector_size=8))
    service = _service(session, gateway)

    async def override_db() -> AsyncIterator[_FakeSession]:
        yield session

    async def override_context() -> WorkspaceExecutionContext:
        return context or _context(session)

    async def override_components():
        return service.retrieval_components

    async def override_gateway():
        return gateway

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_query_workspace_context] = override_context
    app.dependency_overrides[get_retrieval_components] = override_components
    app.dependency_overrides[get_model_gateway] = override_gateway
    return app


@pytest.mark.asyncio
async def test_query_requires_bearer_authentication() -> None:
    session = _FakeSession()
    app = create_app(Settings(testing=True, knowledge_dense_vector_size=8))

    async def override_db() -> AsyncIterator[_FakeSession]:
        yield session

    app.dependency_overrides[get_db_session] = override_db
    response = await _post(app, _payload(session))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.asyncio
async def test_query_returns_citations_and_safe_trace_projection() -> None:
    session = _FakeSession()
    gateway = _FakeModelGateway()
    response = await _post(_app(session, gateway), _payload(session))

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Supported [1]"
    assert body["citations"][0]["id"] == 1
    assert set(body["retrieval_trace"]["stages"]) == {"dense", "sparse", "fused", "rerank"}
    assert "internal-point" not in response.text
    assert "credential" not in response.text.lower()
    assert "provider" not in response.text.lower()


@pytest.mark.asyncio
async def test_query_requires_concrete_snapshot_and_forbids_extra_fields() -> None:
    session = _FakeSession()
    gateway = _FakeModelGateway()
    app = _app(session, gateway)

    latest = _payload(session)
    latest["knowledge_snapshot_id"] = "LATEST"
    extra = _payload(session)
    extra["top_k"] = 20

    latest_response = await _post(app, latest)
    extra_response = await _post(app, extra)

    assert latest_response.status_code == 422
    assert extra_response.status_code == 422
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_query_snapshot_scope_and_permission_are_enforced() -> None:
    session = _FakeSession()
    session.snapshot_exists = False
    gateway = _FakeModelGateway()

    not_found = await _post(_app(session, gateway), _payload(session))
    forbidden = await _post(
        _app(session, gateway, _context(session, permissions=frozenset())),
        _payload(session),
    )

    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_query_model_errors_have_safe_status_and_message() -> None:
    session = _FakeSession()
    gateway = _FakeModelGateway(
        error=ModelGatewayError(
            ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
            message="private provider URL and secret traceback",
        )
    )
    response = await _post(_app(session, gateway), _payload(session))

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "MODEL_PROVIDER_UNAVAILABLE"
    assert "private provider URL" not in response.text
    assert "secret traceback" not in response.text


def test_api_test_fixture_uses_explicit_fake_gateway_not_testing_switch() -> None:
    session = _FakeSession()
    gateway = _FakeModelGateway()
    service = _service(session, gateway)

    assert isinstance(service, CitationQaService)
    assert service.model_gateway is gateway
