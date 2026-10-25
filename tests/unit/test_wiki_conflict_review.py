from __future__ import annotations

import json
from decimal import Decimal
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
    ModelToolCall,
    ModelUsage,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)
from packages.wiki.conflict_review import REVIEW_VERSION, review_conflicts, review_manifest


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="review", trace_id="review"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        workspace_role="DEVELOPER",
        permissions=frozenset({"knowledge_run"}),
    )


def _plan(*, context_tokens: int = 8_192, max_tokens: int = 512) -> ResolvedModelExecutionPlan:
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
                structured_output=False,
                max_context_tokens=context_tokens,
            ),
        )
    )


def _claim(
    value: str,
    *,
    topic: str = "customer records",
    predicate: str = "retention_period",
    condition: str = "",
    quote: str = "Customer records are retained for seven years.",
) -> dict:
    return {
        "topic": topic,
        "kind": "topic",
        "predicate": predicate,
        "value": value,
        "condition": condition,
        "quote": quote,
        "chunk_id": f"chunk-{value}",
        "document_revision_id": "revision-1",
        "locator": {"page": 1},
        "conflict": False,
    }


def _response(
    content: str,
    *,
    usage: ModelUsage | None = None,
    cost: CostEstimate | None = None,
    tool_calls: tuple[ModelToolCall, ...] = (),
) -> ModelResponse:
    return ModelResponse(
        content=content,
        provider="test-provider",
        model="test-model",
        tool_calls=tool_calls,
        usage=usage,
        cost_estimate=cost,
    )


class _Gateway:
    def __init__(self, *responses: ModelResponse) -> None:
        self.responses = list(responses)
        self.calls = []

    async def generate_resolved(self, context, plan, request):
        self.calls.append((context, plan, request))
        return self.responses.pop(0)


def test_manifest_is_stable_and_prompt_uses_prompt_json() -> None:
    manifest = review_manifest()

    assert manifest["version"] == REVIEW_VERSION == "wiki-conflict-review-v1"
    assert manifest["output_schema"]["additionalProperties"] is False
    assert manifest["output_schema"]["required"] == ["groups"]
    assert "UNTRUSTED" in manifest["system_prompt"]
    assert "You have no tools" in manifest["system_prompt"]
    assert "explicitly disjoint" in manifest["system_prompt"]


@pytest.mark.asyncio
async def test_valid_groups_are_bounded_deduplicated_and_canonically_sorted() -> None:
    gateway = _Gateway(
        _response(
            json.dumps({"groups": [[4, 2], [1, 3], [2, 4]]}),
            usage=ModelUsage(input_tokens=40, output_tokens=8, total_tokens=48),
            cost=CostEstimate(amount=Decimal("0.003"), currency="usd"),
        )
    )
    claims = [_claim("seven"), _claim("nine"), _claim("seven"), _claim("nine")]

    groups, telemetry = await review_conflicts(gateway, _plan(), _context(), claims)

    assert groups == [[1, 3], [2, 4]]
    request = gateway.calls[0][2]
    assert request.response_schema is None
    assert request.tools == ()
    assert request.required_capabilities.required == frozenset()
    prompt = json.loads(request.messages[1].content)
    assert prompt["untrusted_claims"][0]["id"] == 1
    assert prompt["untrusted_claims"][0]["quote"] == claims[0]["quote"]
    assert "conflict" not in prompt["untrusted_claims"][0]
    assert telemetry == {
        "provider": "test-provider",
        "model": "test-model",
        "request_count": 1,
        "input_tokens": 40,
        "output_tokens": 8,
        "total_tokens": 48,
        "cost": "0.003",
        "currency": "USD",
        "latency_ms": telemetry["latency_ms"],
    }
    json.dumps((groups, telemetry))


@pytest.mark.asyncio
async def test_model_empty_result_for_disjoint_conditions_is_only_a_candidate() -> None:
    gateway = _Gateway(_response(json.dumps({"groups": []})))
    claims = [
        _claim("seven", condition="EU customers"),
        _claim("nine", condition="US customers"),
    ]

    groups, _ = await review_conflicts(gateway, _plan(), _context(), claims)

    assert groups == []
    prompt = json.loads(gateway.calls[0][2].messages[1].content)
    assert [claim["condition"] for claim in prompt["untrusted_claims"]] == [
        "EU customers",
        "US customers",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        json.dumps({"groups": [[0, 1]]}),
        json.dumps({"groups": [[1, 3]]}),
        json.dumps({"groups": [[1, 1]]}),
        json.dumps({"groups": [[1]]}),
        json.dumps({"groups": [[1, 2]], "truth": "claim 1"}),
        json.dumps({"groups": [[1, "2"]]}),
    ],
)
async def test_invalid_model_outputs_are_rejected(content: str) -> None:
    gateway = _Gateway(_response(content))

    with pytest.raises(AgentHubError) as raised:
        await review_conflicts(
            gateway,
            _plan(),
            _context(),
            [_claim("seven"), _claim("nine")],
        )

    assert raised.value.code == "WIKI_CONFLICT_REVIEW_INVALID_OUTPUT"
    assert raised.value.status_code == 502


@pytest.mark.asyncio
async def test_model_tool_call_is_rejected() -> None:
    gateway = _Gateway(
        _response(
            json.dumps({"groups": [[1, 2]]}),
            tool_calls=(
                ModelToolCall(
                    name="write_truth",
                    arguments={"claim": 1},
                ),
            ),
        )
    )

    with pytest.raises(AgentHubError) as raised:
        await review_conflicts(
            gateway,
            _plan(),
            _context(),
            [_claim("seven"), _claim("nine")],
        )

    assert raised.value.code == "WIKI_CONFLICT_REVIEW_INVALID_OUTPUT"


@pytest.mark.asyncio
async def test_complete_prompt_budget_overflow_rejects_without_model_call() -> None:
    gateway = _Gateway(_response(json.dumps({"groups": []})))
    claims = [_claim("seven", quote="q" * 2_000), _claim("nine", quote="r" * 2_000)]

    with pytest.raises(AgentHubError) as raised:
        await review_conflicts(
            gateway,
            _plan(context_tokens=1_024, max_tokens=256),
            _context(),
            claims,
        )

    assert raised.value.code == "WIKI_CONFLICT_REVIEW_CONTEXT_BUDGET_EXCEEDED"
    assert raised.value.status_code == 422
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_fewer_than_two_claims_return_zero_call_telemetry() -> None:
    gateway = _Gateway(_response(json.dumps({"groups": []})))

    groups, telemetry = await review_conflicts(gateway, _plan(), _context(), [_claim("seven")])

    assert groups == []
    assert telemetry == {
        "provider": "test-provider",
        "model": "test-model",
        "request_count": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost": None,
        "currency": None,
        "latency_ms": 0.0,
    }
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_unknown_usage_and_cost_are_explicit_none_and_json_safe() -> None:
    gateway = _Gateway(_response(json.dumps({"groups": [[1, 2]]})))

    groups, telemetry = await review_conflicts(
        gateway,
        _plan(),
        _context(),
        [_claim("seven"), _claim("nine")],
    )

    assert groups == [[1, 2]]
    assert telemetry["request_count"] == 1
    assert telemetry["input_tokens"] is None
    assert telemetry["output_tokens"] is None
    assert telemetry["total_tokens"] is None
    assert telemetry["cost"] is None
    assert telemetry["currency"] is None
    json.dumps((groups, telemetry))
