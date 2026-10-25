from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import (
    CostEstimate,
    ModelCapabilities,
    ModelResponse,
    ModelUsage,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)
from packages.wiki.compiler import ClaimProposal, Extraction, GatewayWikiCompiler


class FakeGateway:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, ResolvedModelExecutionPlan, Any]] = []

    async def generate_resolved(self, context, plan, request):
        self.calls.append((context, plan, request))
        return self.responses.pop(0)


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="req-1", trace_id="trace-1"),
            organization_id="org-1",
            org_role="member",
        ),
        workspace_id="workspace-1",
        workspace_role="editor",
    )


def _plan(*, context: int = 4096, max_tokens: int = 256) -> ResolvedModelExecutionPlan:
    return ResolvedModelExecutionPlan(
        primary=ResolvedModelExecutionProfile(
            id=uuid4(),
            workspace_id=uuid4(),
            provider_credential_id=uuid4(),
            provider="test-provider",
            model="test-model",
            temperature=Decimal("0"),
            max_tokens=max_tokens,
            timeout_seconds=Decimal("30"),
            capabilities=ModelCapabilities(
                structured_output=True,
                max_context_tokens=context,
            ),
        )
    )


def _chunk(identifier: str = "chunk-1", text: str = "Ada is a mathematician.") -> dict[str, str]:
    return {
        "chunk_id": identifier,
        "text": text,
        "document_id": "doc-1",
        "document_revision_id": "revision-1",
        "locator": "p.1",
    }


def _proposal(
    chunk_id: str = "chunk-1",
    quote: str = "Ada",
    *,
    topic: str = "Ada Lovelace",
) -> dict[str, Any]:
    return {
        "topic": topic,
        "kind": "entity",
        "predicate": "occupation",
        "value": "mathematician",
        "condition": "",
        "chunk_id": chunk_id,
        "quote": quote,
        "related_topics": ["computing"],
    }


def _response(
    *claims: dict[str, Any],
    usage: ModelUsage | None = None,
    cost: CostEstimate | None = None,
) -> ModelResponse:
    return ModelResponse(
        content=json.dumps({"claims": list(claims)}),
        provider="test-provider",
        model="test-model",
        usage=usage,
        cost_estimate=cost,
    )


@pytest.mark.asyncio
async def test_extract_returns_grounded_claims_and_telemetry() -> None:
    gateway = FakeGateway(
        _response(
            _proposal(),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            cost=CostEstimate(amount=Decimal("0.001"), currency="USD"),
        )
    )
    plan = _plan()

    payload, telemetry = await GatewayWikiCompiler(gateway, plan).extract(_context(), [_chunk()])

    assert payload["claims"] == [_proposal()]
    assert Extraction.model_validate(payload, strict=True).claims[0].chunk_id == "chunk-1"
    assert telemetry == {
        "provider": "test-provider",
        "model": "test-model",
        "request_count": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cost": "0.001",
        "currency": "USD",
        "latency_ms": telemetry["latency_ms"],
    }
    assert isinstance(telemetry["latency_ms"], int | float)
    assert telemetry["latency_ms"] >= 0
    calls = gateway.calls
    assert len(calls) == 1
    assert calls[0][1] is plan
    request = calls[0][2]
    assert request.tools == ()
    assert request.response_schema.strict is True
    assert request.response_schema.json_schema["properties"]["claims"]["items"]["properties"][
        "kind"
    ]["enum"] == ["concept", "entity", "topic"]
    assert request.required_capabilities.required == {"structured_output"}
    assert context_is_not_source(calls[0][0], "Ada is a mathematician.")
    source_message = request.messages[1].content
    assert isinstance(source_message, str)
    payload = json.loads(source_message)
    assert payload == {
        "untrusted_source": _chunk(),
    }


def context_is_not_source(context, source: str) -> bool:
    return context.request_id == "req-1" and source not in repr(context)


@pytest.mark.asyncio
async def test_extract_calls_once_per_chunk_and_accumulates_usage() -> None:
    first = _chunk("chunk-1", "First source")
    second = _chunk("chunk-2", "Second source")
    gateway = FakeGateway(
        _response(
            _proposal("chunk-1", "First"),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            cost=CostEstimate(amount=Decimal("0.01"), currency="usd"),
        ),
        _response(
            _proposal("chunk-2", "Second", topic="Second topic"),
            usage=ModelUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            cost=CostEstimate(amount=Decimal("0.02"), currency="USD"),
        ),
    )

    payload, telemetry = await GatewayWikiCompiler(gateway, _plan()).extract(
        _context(), [first, second]
    )

    assert [claim["chunk_id"] for claim in payload["claims"]] == [
        "chunk-1",
        "chunk-2",
    ]
    assert len(gateway.calls) == 2
    assert json.loads(gateway.calls[1][2].messages[1].content)["untrusted_source"] == second
    assert telemetry["request_count"] == 2
    assert telemetry["input_tokens"] == 6
    assert telemetry["output_tokens"] == 3
    assert telemetry["total_tokens"] == 9
    assert telemetry["cost"] == "0.03"
    assert telemetry["currency"] == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    [
        _proposal("other-chunk", "Ada"),
        _proposal("chunk-1", "Ada invented the first compiler."),
        _proposal("chunk-1", "ada"),
    ],
)
async def test_extract_rejects_fabricated_chunk_or_quote(claim: dict[str, Any]) -> None:
    gateway = FakeGateway(_response(claim))

    with pytest.raises(AgentHubError) as raised:
        await GatewayWikiCompiler(gateway, _plan()).extract(_context(), [_chunk()])

    assert raised.value.code == "WIKI_COMPILER_INVALID_OUTPUT"
    assert raised.value.status_code == 502


@pytest.mark.asyncio
async def test_extract_accepts_no_claims_and_reports_unknown_usage_as_none() -> None:
    gateway = FakeGateway(_response())

    payload, telemetry = await GatewayWikiCompiler(gateway, _plan()).extract(_context(), [_chunk()])

    assert payload == {"claims": []}
    assert telemetry["request_count"] == 1
    assert telemetry["input_tokens"] is None
    assert telemetry["output_tokens"] is None
    assert telemetry["total_tokens"] is None
    assert telemetry["cost"] is None
    assert telemetry["currency"] is None


@pytest.mark.asyncio
async def test_extract_rejects_chunk_above_frozen_input_budget_without_model_call() -> None:
    gateway = FakeGateway(_response())
    plan = _plan(context=128, max_tokens=16)

    with pytest.raises(AgentHubError) as raised:
        await GatewayWikiCompiler(gateway, plan).extract(_context(), [_chunk(text="x" * 4096)])

    assert raised.value.code == "WIKI_COMPILER_CONTEXT_BUDGET_EXCEEDED"
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(
            content="not-json",
            provider="test-provider",
            model="test-model",
        ),
        ModelResponse(
            content=json.dumps({"claims": [], "extra": True}),
            provider="test-provider",
            model="test-model",
        ),
        ModelResponse(
            content=json.dumps(
                {
                    "claims": [
                        {
                            **_proposal(),
                            "topic": 7,
                        }
                    ]
                }
            ),
            provider="test-provider",
            model="test-model",
        ),
    ],
)
async def test_extract_rejects_invalid_model_schema(response: ModelResponse) -> None:
    gateway = FakeGateway(response)

    with pytest.raises(AgentHubError) as raised:
        await GatewayWikiCompiler(gateway, _plan()).extract(_context(), [_chunk()])

    assert raised.value.code == "WIKI_COMPILER_INVALID_OUTPUT"


def test_claim_proposal_and_extraction_are_strict_and_bounded() -> None:
    claim = ClaimProposal(**_proposal())
    assert claim.condition == ""
    assert Extraction(claims=[claim]).claims == [claim]

    with pytest.raises(ValueError):
        ClaimProposal(**_proposal(), unexpected=True)
    with pytest.raises(ValueError):
        ClaimProposal(**{**_proposal(), "topic": "x" * 121})
    with pytest.raises(ValueError):
        ClaimProposal(**{**_proposal(), "related_topics": ["x"] * 13})
    with pytest.raises(ValueError):
        Extraction(claims=[_proposal()] * 101)


@pytest.mark.asyncio
async def test_prompt_json_preserves_capability_and_validates_real_locator():
    gateway = FakeGateway(_response(_proposal()))
    chunk = {**_chunk(), "locator": {"page": 1}}
    payload, telemetry = await GatewayWikiCompiler(
        gateway, _plan(context=8192), output_mode="PROMPT_JSON"
    ).extract(_context(), [chunk])
    assert payload["claims"][0]["quote"] == "Ada"
    request = gateway.calls[0][2]
    assert request.response_schema is None
    assert not request.required_capabilities.required
    assert '"page":1' in request.messages[1].content
    assert telemetry["cost"] is None


@pytest.mark.asyncio
async def test_cost_is_json_serializable_for_persistence():
    gateway = FakeGateway(_response(_proposal(), cost=CostEstimate(Decimal("0.01"), "USD")))
    _, telemetry = await GatewayWikiCompiler(gateway, _plan()).extract(_context(), [_chunk()])
    assert json.loads(json.dumps(telemetry))["cost"] == "0.01"
