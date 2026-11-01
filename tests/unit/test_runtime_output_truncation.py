from __future__ import annotations

import pytest

from packages.agent_runtime.events import AgentEventType
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
)
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec, _model_graph


def _state(graph):
    return {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            ModelMessage(role="user", content="question"),
        ],
        "spec": _frozen_spec(20_000),
        "runtime": {"max_steps": 5, "context_budget": graph._runtime_context_budget},
        "tool_definitions": {},
        "model_round_count": 0,
        "usage_records": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_length_finish_is_failed_and_never_completed(streaming):
    graph = _model_graph(request_sink=[])
    graph.mode = "stream" if streaming else "sync"
    events = []
    steps = []

    async def emit(event_type, data):
        events.append((event_type, data))

    async def step(name, status, metadata):
        steps.append((name, status, metadata))

    response = ModelResponse(
        content="partial answer",
        provider="fake",
        model="test-model",
        finish_reason="length",
    )

    class Gateway:
        async def generate_resolved(self, _context, _plan, _request):
            return response

        async def stream_resolved(self, _context, _plan, _request):
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta=response.content,
            )
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.COMPLETED,
                response=response,
            )

    graph.service.model_gateway_factory = lambda _session: Gateway()
    graph._emit = emit
    graph.step = step

    state = _state(graph)
    result = await graph.model(state)

    assert result["failure_code"] == "MODEL_OUTPUT_TRUNCATED"
    assert "model_response" not in result
    assert result.get("final_output") is None
    assert result["usage_records"] == [{"usage": None, "cost": None}]
    assert ("MODEL", "FAILED", {
        "model_round": 1,
        "error_code": "MODEL_OUTPUT_TRUNCATED",
        "finish_reason": "length",
    }) in steps
    assert all(event_type is not AgentEventType.MESSAGE_COMPLETED for event_type, _ in events)
    terminal_state = {**state, **result}
    assert graph.after_model(terminal_state) == "finish"
    await graph.finish(terminal_state)
    assert graph.pending_finish["status"] == "FAILED"
    assert graph.pending_finish["safe_metadata"]["error_code"] == "MODEL_OUTPUT_TRUNCATED"
    if streaming:
        assert [event_type for event_type, _ in events] == [
            AgentEventType.CONTEXT_BUDGET,
            AgentEventType.MESSAGE_STARTED,
            AgentEventType.MESSAGE_DELTA,
        ]
        assert events[-1] == (AgentEventType.MESSAGE_DELTA, {"delta": "partial answer"})


@pytest.mark.asyncio
async def test_empty_output_remains_an_empty_response_failure():
    graph = _model_graph(request_sink=[])

    class Gateway:
        async def generate_resolved(self, _context, _plan, _request):
            return ModelResponse(content="", provider="fake", model="test-model")

    graph.service.model_gateway_factory = lambda _session: Gateway()
    result = await graph.model(_state(graph))

    assert result["failure_code"] == "AGENT_MODEL_EMPTY_RESPONSE"
    assert result["failure_code"] != "MODEL_OUTPUT_TRUNCATED"
