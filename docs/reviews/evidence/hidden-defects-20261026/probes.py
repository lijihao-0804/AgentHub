"""Offline probes: assertions describe defects observed at 4d07c87, not desired behavior."""

import json

import pytest
from pydantic import ValidationError

from apps.api.routes.citation_qa import _response
from packages.agent_runtime.context_budget import ContextCategory
from packages.agent_runtime.runtime_projection import _bounded_tool_result, _categorize_messages
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.tools.contracts import ToolResult
from tests.unit.test_knowledge_m3e_citation_qa import (
    _answer,
    _FakeModelGateway,
    _FakeSession,
    _service,
)
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec, _graph


def test_history_count_becomes_stale_after_first_admission():
    graph = _graph()
    state = {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            *[
                m
                for i in range(4)
                for m in (
                    ModelMessage(role="user", content=f"old question {i}" + "x" * 120),
                    ModelMessage(role="assistant", content=f"old answer {i}" + "y" * 120),
                )
            ],
            ModelMessage(role="user", content="CURRENT TASK" * 8),
        ],
        "spec": _frozen_spec(200),
        "runtime": {
            "context_budget": {
                "reserved_output_tokens": 1,
                "max_retrieval_tokens": 100,
                "max_tool_result_tokens": 100,
            }
        },
        "tool_definitions": {},
        "history_message_count": 8,
        "memory_message_count": 0,
    }
    first = graph._admit_context(state)
    assert first.usage.dropped_exchange_count > 0
    state["messages"] = list(first.messages)
    classified = _categorize_messages(state["messages"], history_message_count=8)
    task = next(m for m in classified if m.content == "CURRENT TASK" * 8)
    assert task.category is ContextCategory.CONVERSATION
    state["messages"].extend(
        [
            ModelMessage(
                role="assistant",
                content="x" * 320,
                tool_calls=(
                    ModelToolCall(name="lookup", arguments={}, provider_tool_call_id="call-1"),
                ),
            ),
            ModelMessage(
                role="tool",
                name="lookup",
                tool_call_id="call-1",
                content=json.dumps({"status": "SUCCESS", "data": "y" * 300}),
            ),
        ]
    )
    second = graph._admit_context(state)
    assert not any(m.content == "CURRENT TASK" * 8 for m in second.messages)
    assert any(m.role == "tool" for m in second.messages)


@pytest.mark.asyncio
async def test_valid_1200_char_citation_breaks_api_response():
    session = _FakeSession(evidence_text="e" * 1200)
    gateway = _FakeModelGateway(
        '{"answer":"Supported [1]","citation_ids":[1],"insufficient_evidence":false}'
    )
    result = await _answer(_service(session, gateway), session, gateway)
    assert len(result.citations[0].excerpt) == 1200
    with pytest.raises(ValidationError, match="1000 characters"):
        _response(result)


@pytest.mark.asyncio
async def test_citation_qa_dispatches_without_context_admission():
    from packages.agent_runtime.context_budget import ApproxTokenEstimator

    session = _FakeSession(chunks=6)
    for chunk in session.chunks:
        chunk.text = "word " * 400  # 2000 chars: within configured ingestion ceiling.
    gateway = _FakeModelGateway(
        '{"answer":"Supported [1]","citation_ids":[1],"insufficient_evidence":false}'
    )
    await _answer(_service(session, gateway), session, gateway)
    request = gateway.requests[0]
    estimate = sum(ApproxTokenEstimator().estimate(m.content or "") for m in request.messages)
    assert estimate > 4096 - 2048  # Valid 4k profile with a 2k output allowance.
    print(
        json.dumps(
            {
                "citation_qa_dispatched_input_estimate": estimate,
                "gateway_capability_or_resolved_plan_checked": False,
            }
        )
    )


@pytest.mark.parametrize("budget,length", [(100, 330), (4000, 15930)])
def test_generic_tool_result_near_pool_limit_loses_entire_content(budget, length):
    graph = _graph()
    payload = _bounded_tool_result(
        ToolResult.success({"value": "x" * length}), max_tool_result_tokens=budget
    )
    assert payload["data"]["value"] == "x" * length
    state = {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(name="lookup", arguments={}, provider_tool_call_id="call-1"),
                ),
            ),
            ModelMessage(
                role="tool",
                name="lookup",
                tool_call_id="call-1",
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        ],
        "spec": _frozen_spec(20000),
        "tool_definitions": {},
        "runtime": {
            "context_budget": {
                "reserved_output_tokens": 1,
                "max_retrieval_tokens": 100,
                "max_tool_result_tokens": budget,
            }
        },
    }
    admission = graph._admit_context(state)
    result = json.loads(admission.messages[-1].content)
    assert result == {"truncated": True, "trust": "UNTRUSTED"}


@pytest.mark.asyncio
async def test_1200_char_citation_is_http_500():
    import httpx

    from tests.unit.test_knowledge_m3e_api import _app, _payload

    session = _FakeSession(evidence_text="e" * 1200)
    gateway = _FakeModelGateway()
    transport = httpx.ASGITransport(app=_app(session, gateway), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/knowledge/query", json=_payload(session))
    assert response.status_code == 500
