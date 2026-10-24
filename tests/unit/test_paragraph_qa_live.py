from __future__ import annotations

import copy
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks.public_rag.paragraph_qa_live import (
    RouteClient,
    empty_result,
    report_for,
    successful_search_observation,
)
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import ConfiguredRoute, Endpoint, build_gateway
from packages.model_gateway.contracts import ModelMessage, ModelRequest, RetryPolicy

PRIMARY_BASE = "http://127.0.0.1:3425/v1"
FALLBACK_BASE = "https://fallback.invalid/v1"
PRIMARY_MODEL = "auto/deepseek-v4.1-flash"
FALLBACK_MODEL = "deepseek-v4.1-flash-1017"
TEXT = "The result is alpha."


def _route() -> dict[str, list[dict[str, str]]]:
    return {
        "chain": [
            {"name": "local-magpie", "base_url": PRIMARY_BASE, "model": PRIMARY_MODEL},
            {"name": "aliyun-rag", "base_url": FALLBACK_BASE, "model": FALLBACK_MODEL},
        ]
    }


def _request(api_base: str, model: str) -> dict[str, Any]:
    return {
        "api_base": api_base,
        "model": "openai/" + model,
        "max_tokens": 1024,
        "num_retries": 0,
        "temperature": 0,
    }


@pytest.mark.asyncio
async def test_dispatch_budget_is_shared_across_separate_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    dispatched: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        dispatched.append(kwargs)
        return SimpleNamespace(model=FALLBACK_MODEL)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    budget = {"dispatched": 0, "limit": 1}
    first = RouteClient(StringIO(), {"query_id": "q1"}, budget, _route(), [])
    second = RouteClient(StringIO(), {"query_id": "q2"}, budget, _route(), [])

    await first.acompletion(**_request(FALLBACK_BASE, FALLBACK_MODEL))
    with pytest.raises(ValueError, match="physical dispatch ceiling reached"):
        await second.acompletion(**_request(FALLBACK_BASE, FALLBACK_MODEL))

    assert len(dispatched) == 1
    assert budget == {"dispatched": 1, "limit": 1}
    assert first.halted is False
    assert second.halted is True


@pytest.mark.asyncio
async def test_wrong_provider_model_identity_halts_future_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    async def fake_acompletion(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(model="unexpected-provider-model")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    budget = {"dispatched": 0, "limit": 2}
    client = RouteClient(StringIO(), {}, budget, _route(), [])

    with pytest.raises(ValueError, match="unexpected raw provider model identity"):
        await client.acompletion(**_request(PRIMARY_BASE, PRIMARY_MODEL))

    assert client.halted is True
    assert client.raw_calls[0]["status"] == "MODEL_IDENTITY_ERROR"
    assert client.raw_calls[0]["raw_response_model"] == "unexpected-provider-model"
    with pytest.raises(ValueError, match="physical dispatch ceiling reached"):
        await client.acompletion(**_request(FALLBACK_BASE, FALLBACK_MODEL))
    assert budget["dispatched"] == 1


@pytest.mark.asyncio
async def test_primary_connection_failure_does_not_halt_gateway_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        if kwargs["api_base"] == PRIMARY_BASE:
            raise litellm.APIConnectionError(
                message="primary offline",
                llm_provider="openai",
                model=PRIMARY_MODEL,
            )
        return SimpleNamespace(
            model=FALLBACK_MODEL,
            choices=[
                {
                    "message": {"content": "fallback answer"},
                    "finish_reason": "stop",
                }
            ],
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    client = RouteClient(StringIO(), {}, {"dispatched": 0, "limit": 2}, _route(), [])
    adapter = LiteLLMProviderAdapter(client=client)
    context = _context()
    gateway, primary_id = build_gateway(_configured_route(), context, adapter=adapter)

    response = await gateway.generate(
        context,
        primary_id,
        ModelRequest(
            messages=(ModelMessage(role="user", content="question"),),
            retry_policy=RetryPolicy(max_attempts=1),
        ),
    )

    assert response.content == "fallback answer"
    assert response.model == FALLBACK_MODEL
    assert client.halted is False
    assert [call["api_base"] for call in calls] == [PRIMARY_BASE, FALLBACK_BASE]
    assert [record["status"] for record in client.raw_calls] == [
        "TRANSPORT_UNCONFIRMED",
        "RECEIVED",
    ]


def _configured_route() -> ConfiguredRoute:
    return ConfiguredRoute(
        schema_version=1,
        name="local-rag",
        primary=_endpoint("local-magpie", PRIMARY_BASE, PRIMARY_MODEL),
        fallbacks=(_endpoint("aliyun-rag", FALLBACK_BASE, FALLBACK_MODEL),),
    )


def _endpoint(name: str, base_url: str, model: str) -> Endpoint:
    return Endpoint(
        name=name,
        provider="openai-compatible",
        model=model,
        base_url=base_url,
        api_key=name + "-secret",
        temperature=0,
        max_tokens=1024,
        timeout_seconds=20,
        capabilities={
            "tool_calling": True,
            "streaming": True,
            "structured_output": False,
            "vision": False,
            "max_context_tokens": 32768,
        },
    )


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="route-test", trace_id="route-test"),
            organization_id="00000000-0000-0000-0000-000000000002",
            org_role="OWNER",
        ),
        workspace_id="00000000-0000-0000-0000-000000000001",
        workspace_role="DEVELOPER",
    )


def test_report_for_renames_arms_and_roles_without_losing_direction() -> None:
    data, rows, sources = _report_fixture()

    report = report_for(data, rows, sources)

    assert set(report["arms"]) == {"fixed", "structural"}
    assert report["protocol"]["baseline_arm"] == "fixed"
    assert report["protocol"]["candidate_arm"] == "structural"
    assert report["protocol"]["direction"] == "structural minus fixed"
    assert report["arm_roles"] == {
        "fixed": "existing1200/200 character chunks",
        "structural": "native paragraphs/sentences <=1000, no overlap",
    }
    assert report["cases"][0]["arms"]["fixed"]["arm_name"] == "fixed"
    assert report["cases"][0]["arms"]["structural"]["arm_name"] == "structural"


def test_report_for_keeps_all_planned_failures_in_denominator() -> None:
    data, rows, sources = _report_fixture()
    rows[0]["arms"]["structural"] = empty_result()

    report = report_for(data, rows, sources)

    assert report["denominators"]["planned_rows"] == 2
    assert report["denominators"]["primary_rows"] == 2
    fixed = report["arms"]["fixed"]["primary"]
    structural = report["arms"]["structural"]["primary"]
    assert fixed["planned_queries"] == structural["planned_queries"] == 2
    assert fixed["failure_count"] == 0
    assert fixed["valid_response_fraction"] == 1
    assert structural["failure_count"] == structural["not_attempted_count"] == 1
    assert structural["valid_response_fraction"] == 0.5
    assert structural["token_f1"] == 0.5


def _report_fixture() -> tuple[dict, list[dict], dict]:
    cases = [
        _case("q1", "answerable", TEXT),
        _case("q2", "answerable", "A second result is beta."),
    ]
    sources = {
        "chunk-q1": {
            "paper_id": "paper-1",
            "chunk_text": TEXT,
            "paragraphs": [{"key": "paper-1:p1", "text": TEXT}],
        },
        "chunk-q2": {
            "paper_id": "paper-1",
            "chunk_text": "A second result is beta.",
            "paragraphs": [{"key": "paper-1:p2", "text": "A second result is beta."}],
        },
    }
    rows = [
        _row("q1", "chunk-q1", "alpha", TEXT),
        _row("q2", "chunk-q2", "beta", "A second result is beta."),
    ]
    return {"cases": cases, "corpus": {}}, rows, sources


def _case(query_id: str, consensus: str, evidence: str) -> dict:
    return {
        "query_id": query_id,
        "paper_id": "paper-1",
        "question": "What is the result?",
        "annotations": [
            {
                "answer_type": "extractive",
                "answer": "alpha" if query_id == "q1" else "beta",
                "evidence": [evidence],
            }
        ],
        "consensus": consensus,
        "scorable": True,
    }


def _row(query_id: str, chunk_id: str, answer: str, snippet: str) -> dict:
    arm = {
        "run_id": "run-" + query_id,
        "run_status": "SUCCEEDED",
        "failure_code": None,
        "answer": {
            "answer": answer,
            "unanswerable": False,
            "cited_chunk_ids": [chunk_id],
        },
        "visible_records": [{"chunk_id": chunk_id, "snippet": snippet}],
        "model_calls": [],
        "latency_ms": 100.0,
        "tool_calls": 1,
        "run_usage": {},
        "workflow_valid": True,
    }
    return {
        "query_id": query_id,
        "paper_id": "paper-1",
        "arms": {"fixed": copy.deepcopy(arm), "structural": copy.deepcopy(arm)},
    }


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"status":"SUCCESS","data":{"truncated":true,"preview":"partial data"}}', True),
        ('{"status":"ERROR","error_code":"TOOL_TIMEOUT"}', False),
        ("[]", False),
    ],
)
def test_search_trace_requires_successful_observation_delivery(content, expected):
    calls = [
        {"status": "VALID", "tool_calls": [], "messages": [{"role": "tool", "content": content}]}
    ]
    assert successful_search_observation(calls) is expected


@pytest.mark.asyncio
async def test_cancelled_dispatch_retains_unconfirmed_outcome(monkeypatch):
    import asyncio
    import json

    import litellm

    async def cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(litellm, "acompletion", cancelled)
    events = StringIO()
    client = RouteClient(events, {}, {"dispatched": 0, "limit": 1}, _route(), [])
    with pytest.raises(asyncio.CancelledError):
        await client.acompletion(**_request(PRIMARY_BASE, PRIMARY_MODEL))
    assert client.raw_calls[0]["status"] == "TRANSPORT_UNCONFIRMED"
    assert json.loads(events.getvalue().splitlines()[-1])["error_type"] == "CancelledError"
