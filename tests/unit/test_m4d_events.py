from __future__ import annotations

import asyncio
import json

import pytest

from packages.agent_runtime.events import AgentEventEmitter, AgentEventType
from packages.agent_runtime.sse import event_to_sse


@pytest.mark.asyncio
async def test_event_emitter_sequences_events_per_run() -> None:
    emitter = AgentEventEmitter(
        run_id="run-1", agent_version_id="version-1", request_id="request-1"
    )
    events = await asyncio.gather(
        *(emitter.emit(AgentEventType.MESSAGE_DELTA, {"delta": str(index)}) for index in range(10))
    )
    assert sorted(event.sequence for event in events) == list(range(1, 11))
    assert {event.run_id for event in events} == {"run-1"}
    assert len({event.event_id for event in events}) == 10
    assert {event.request_id for event in events} == {"request-1"}


@pytest.mark.asyncio
async def test_sse_frame_is_valid_json_and_has_event_type() -> None:
    emitter = AgentEventEmitter(run_id="run-1", agent_version_id="version-1")
    event = await emitter.emit(
        AgentEventType.CONTEXT_BUDGET,
        {
            "model_round": 1,
            "context_limit": 8192,
            "reserved_output": 256,
            "estimated_input_before": 100,
            "estimated_input_after": 90,
            "truncated": True,
            "dropped_exchange_count": 1,
        },
    )
    frame = event_to_sse(event)
    assert frame.startswith("event: context.budget\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("\n", 2)[1][len("data: ") :])
    assert payload["type"] == "context.budget"
    assert payload["payload"]["truncated"] is True
    assert "data" not in payload


@pytest.mark.asyncio
async def test_tool_events_reject_arguments_and_results() -> None:
    emitter = AgentEventEmitter(run_id="run-1", agent_version_id="version-1")
    with pytest.raises(ValueError, match="unsafe event fields"):
        await emitter.emit(
            AgentEventType.TOOL_STARTED,
            {"tool_call_id": "call-1", "tool_identity": "calculator", "arguments": {"x": 1}},
        )
    with pytest.raises(ValueError, match="unsafe event fields"):
        await emitter.emit(
            AgentEventType.TOOL_COMPLETED,
            {
                "tool_call_id": "call-1",
                "tool_identity": "calculator",
                "status": "SUCCEEDED",
                "result": {"secret": "no"},
                "duration_ms": 1,
            },
        )


@pytest.mark.asyncio
async def test_failed_event_contains_only_safe_failure_projection() -> None:
    emitter = AgentEventEmitter(run_id="run-1", agent_version_id="version-1")
    event = await emitter.emit(
        AgentEventType.RUN_FAILED,
        {
            "status": "FAILED",
            "failure_code": "MODEL_STREAM_INTERRUPTED",
            "model_step_count": 1,
            "tool_call_count": 0,
        },
    )
    assert event.as_dict()["payload"] == {
        "status": "FAILED",
        "failure_code": "MODEL_STREAM_INTERRUPTED",
        "model_step_count": 1,
        "tool_call_count": 0,
    }
    assert "data" not in event.as_dict()
    with pytest.raises(ValueError, match="unsafe event fields"):
        await emitter.emit(
            AgentEventType.RUN_FAILED,
            {
                "status": "FAILED",
                "failure_code": "MODEL_BAD_RESPONSE",
                "exception": "raw provider details",
            },
        )
