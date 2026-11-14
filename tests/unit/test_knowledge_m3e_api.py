from __future__ import annotations

import logging
from contextlib import nullcontext
from uuid import uuid4

import httpx
import pytest

from apps.api.app import create_app
from apps.api.knowledge_dependencies import (
    get_citation_qa_generator,
    get_citation_qa_model_plan_resolver,
    get_query_workspace_context,
    get_retrieval_components,
)
from packages.core.config.settings import Settings
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.citation_qa import CitationQaService
from packages.model_gateway.contracts import AliyunDeepSeekRequestMode, ModelResponse
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import _validate_usable_response
from tests.unit.test_knowledge_m3e_citation_qa import (
    _context,
    _FakeModelGateway,
    _FakeSession,
    _resolve_plan,
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

    app.state.db_session_factory = lambda: nullcontext(session)

    async def override_context() -> WorkspaceExecutionContext:
        return context or _context(session)

    async def override_components():
        return service.retrieval_components

    async def override_plan_resolver():
        return _resolve_plan

    app.dependency_overrides[get_query_workspace_context] = override_context
    app.dependency_overrides[get_retrieval_components] = override_components
    app.dependency_overrides[get_citation_qa_generator] = lambda: gateway
    app.dependency_overrides[get_citation_qa_model_plan_resolver] = override_plan_resolver
    return app


@pytest.mark.asyncio
async def test_query_requires_bearer_authentication() -> None:
    session = _FakeSession()
    app = create_app(Settings(testing=True, knowledge_dense_vector_size=8))

    app.state.db_session_factory = lambda: nullcontext(session)

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
    assert body["insufficient_evidence"] is False
    assert body["citations"][0]["id"] == 1
    assert set(body["retrieval_trace"]["stages"]) == {
        "dense",
        "sparse",
        "fused",
        "rerank",
        "selection",
    }
    assert "internal-point" not in response.text
    assert "credential" not in response.text.lower()
    assert "provider" not in response.text.lower()
    assert gateway.requests[0].request_mode is AliyunDeepSeekRequestMode.PROVIDER_DEFAULT


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_explicit_prompt_json_uses_strict_local_validation(valid: bool) -> None:
    session = _FakeSession()
    gateway = _FakeModelGateway(
        '{"answer":"Source [1]","citation_ids":[1],"insufficient_evidence":false}'
        if valid
        else '{"answer":"not valid enough"}'
    )
    response = await _post(
        _app(session, gateway),
        _payload(session) | {"output_mode": "PROMPT_JSON"},
    )
    [sent] = gateway.requests
    assert sent.request_mode is AliyunDeepSeekRequestMode.PROVIDER_DEFAULT
    assert sent.response_schema is None
    assert "structured_output" not in sent.required_capabilities.required
    assert '"insufficient_evidence"' in sent.messages[0].content
    if valid:
        assert response.status_code == 200 and response.json()["citations"][0]["id"] == 1
    else:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "CITATION_QA_INVALID_MODEL_OUTPUT"
        assert "schema validation" in response.json()["error"]["message"]


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
    unknown_mode = await _post(app, _payload(session) | {"output_mode": "AUTO"})
    unknown_request_mode = await _post(app, _payload(session) | {"request_mode": "non_thinking"})

    assert latest_response.status_code == 422
    assert extra_response.status_code == 422
    assert unknown_mode.status_code == 422
    assert unknown_request_mode.status_code == 422
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_empty_snapshot_reports_insufficient_evidence_without_model_call():
    session = _FakeSession(chunks=0)
    gateway = _FakeModelGateway()
    response = await _post(_app(session, gateway), _payload(session))
    assert response.status_code == 200
    assert response.json()["insufficient_evidence"] is True and response.json()["citations"] == []
    assert not gateway.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["JSON_SCHEMA", "PROMPT_JSON"])
@pytest.mark.parametrize("duplicate", ['"insufficient_evidence":true,', '"answer":"unverified",'])
async def test_repeated_json_fields_are_ambiguous(mode, duplicate):
    session = _FakeSession()
    gateway = _FakeModelGateway(
        "{" + duplicate + '"answer":"Source [1]","citation_ids":[1],"insufficient_evidence":false}'
    )
    response = await _post(_app(session, gateway), _payload(session) | {"output_mode": mode})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CITATION_QA_INVALID_MODEL_OUTPUT"
    assert "strict JSON parsing" in response.json()["error"]["message"]
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason", "code", "diagnostic", "failure_code"),
    [
        (
            "",
            "length",
            "MODEL_BAD_RESPONSE",
            "output limit without visible content",
            "gateway_empty_output",
        ),
        ("", "stop", "MODEL_BAD_RESPONSE", "no visible content", "gateway_empty_output"),
        (
            '{"private":',
            "length",
            "CITATION_QA_INVALID_MODEL_OUTPUT",
            "model output limit",
            "json_parse",
        ),
        (
            '{"private":1}',
            "stop",
            "CITATION_QA_INVALID_MODEL_OUTPUT",
            "schema validation",
            "schema_validation",
        ),
        (
            '{"answer":"Private","citation_ids":[1],"insufficient_evidence":false}',
            "stop",
            "CITATION_QA_INVALID_MODEL_OUTPUT",
            "citation IDs must exist and agree",
            "citation_mismatch",
        ),
    ],
)
async def test_query_reports_safe_failure_stage_without_retry(
    caplog, content, finish_reason, code, diagnostic, failure_code
):
    class Gateway(_FakeModelGateway):
        async def generate_resolved(self, context, plan, request):
            self.requests.append(request)
            result = ModelResponse(
                content, provider="fake", model="fake", finish_reason=finish_reason
            )
            _validate_usable_response(result)
            return result

    session = _FakeSession()
    gateway = Gateway()
    with caplog.at_level(logging.INFO, logger="agenthub.observability"):
        response = await _post(_app(session, gateway), _payload(session))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == code
    assert diagnostic in response.json()["error"]["message"]
    assert "private" not in response.text.lower()
    assert len(gateway.requests) == 1
    [trace] = [
        rec.trace_span
        for rec in caplog.records
        if getattr(rec, "trace_span", {}).get("span") == "knowledge.qa.output_failure"
    ]
    assert trace["failure_code"] == failure_code
    assert trace["attempt"] == 1
    assert trace["output_limit_reached"] is (finish_reason == "length")
    assert "private" not in str(trace).lower()


@pytest.mark.asyncio
async def test_oversized_citation_id_is_rejected_after_single_correction(caplog):
    session = _FakeSession()
    marker = "9" * 4600
    gateway = _FakeModelGateway(
        f'{{"answer":"Private [{marker}]","citation_ids":[1],"insufficient_evidence":false}}'
    )
    with caplog.at_level(logging.INFO, logger="agenthub.observability"):
        response = await _post(_app(session, gateway), _payload(session))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CITATION_QA_INVALID_MODEL_OUTPUT"
    assert len(gateway.requests) == 2
    traces = [
        rec.trace_span
        for rec in caplog.records
        if getattr(rec, "trace_span", {}).get("span") == "knowledge.qa.output_failure"
    ]
    assert [(row["failure_code"], row["attempt"]) for row in traces] == [
        ("citation_out_of_range", 1),
        ("citation_out_of_range", 2),
    ]
    assert "private" not in str(traces).lower()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("excerpt_chars", [1_000, 1_001, 1_200, 2_000])
async def test_query_accepts_every_supported_excerpt_boundary(
    excerpt_chars: int,
) -> None:
    session = _FakeSession(evidence_text="e" * excerpt_chars)
    gateway = _FakeModelGateway()

    response = await _post(_app(session, gateway), _payload(session))

    assert response.status_code == 200
    assert response.json()["citations"][0]["excerpt"] == "e" * excerpt_chars
