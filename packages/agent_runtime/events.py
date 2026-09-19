"""Provider-neutral, safe AgentHub run events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class AgentEventType(StrEnum):
    RUN_STARTED = "run.started"
    MESSAGE_STARTED = "message.started"
    CONTEXT_BUDGET = "context.budget"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    RETRIEVAL_STARTED = "retrieval.started"
    RETRIEVAL_COMPLETED = "retrieval.completed"
    RERANK_COMPLETED = "rerank.completed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_RESOLVED = "approval.resolved"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    USAGE = "usage"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


_ALLOWED_FIELDS: dict[AgentEventType, frozenset[str]] = {
    AgentEventType.RUN_STARTED: frozenset({"status", "model_step_count", "tool_call_count"}),
    AgentEventType.MESSAGE_STARTED: frozenset({"model_round"}),
    AgentEventType.CONTEXT_BUDGET: frozenset(
        {
            "model_round",
            "context_limit",
            "reserved_output",
            "estimated_input_before",
            "estimated_input_after",
            "truncated",
            "dropped_exchange_count",
        }
    ),
    AgentEventType.MESSAGE_DELTA: frozenset({"delta"}),
    AgentEventType.MESSAGE_COMPLETED: frozenset({"model_round", "status"}),
    AgentEventType.RETRIEVAL_STARTED: frozenset(),
    AgentEventType.RETRIEVAL_COMPLETED: frozenset({"duration_ms", "result_count"}),
    AgentEventType.RERANK_COMPLETED: frozenset({"duration_ms", "result_count"}),
    AgentEventType.TOOL_REQUESTED: frozenset({"tool_call_id", "tool_identity"}),
    AgentEventType.TOOL_STARTED: frozenset({"tool_call_id", "tool_identity"}),
    AgentEventType.TOOL_COMPLETED: frozenset(
        {"tool_call_id", "tool_identity", "status", "error_code", "duration_ms"}
    ),
    AgentEventType.TOOL_FAILED: frozenset(
        {"tool_call_id", "tool_identity", "error_code", "duration_ms"}
    ),
    AgentEventType.APPROVAL_REQUIRED: frozenset(
        {
            "approval_id",
            "logical_action_id",
            "tool_identity",
            "risk_level",
            "decision_status",
            "execution_status",
        }
    ),
    AgentEventType.APPROVAL_RESOLVED: frozenset(
        {"approval_id", "decision_status", "execution_status", "failure_code"}
    ),
    AgentEventType.RUN_CANCEL_REQUESTED: frozenset({"status"}),
    AgentEventType.USAGE: frozenset(
        {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
    ),
    AgentEventType.RUN_COMPLETED: frozenset(
        {"status", "model_step_count", "tool_call_count", "output"}
    ),
    AgentEventType.RUN_FAILED: frozenset(
        {"status", "failure_code", "model_step_count", "tool_call_count"}
    ),
    AgentEventType.RUN_CANCELLED: frozenset(
        {"status", "model_step_count", "tool_call_count"}
    ),
}

_NUMERIC_FIELDS = frozenset(
    {
        "model_round",
        "context_limit",
        "reserved_output",
        "estimated_input_before",
        "estimated_input_after",
        "dropped_exchange_count",
        "model_step_count",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "duration_ms",
        "result_count",
    }
)


def _validated_data(event_type: AgentEventType, data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError("event data must be a mapping")
    allowed = _ALLOWED_FIELDS[event_type]
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unsafe event fields: {', '.join(sorted(unknown))}")
    result = dict(data)
    for key, value in result.items():
        if key in _NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"event field {key} must be numeric")
        elif key == "truncated":
            if not isinstance(value, bool):
                raise ValueError("event field truncated must be boolean")
        elif value is not None and not isinstance(value, str):
            raise ValueError(f"event field {key} must be a scalar")
    return result


class AgentEvent:
    """Immutable event envelope with a safe, provider-neutral payload."""

    __slots__ = (
        "event_id",
        "request_id",
        "step_id",
        "sequence",
        "type",
        "run_id",
        "agent_version_id",
        "timestamp",
        "payload",
    )

    def __init__(
        self,
        *,
        sequence: int,
        type: AgentEventType | str,
        request_id: str,
        run_id: str,
        agent_version_id: str,
        step_id: str | None = None,
        event_id: str | None = None,
        timestamp: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        if sequence < 1:
            raise ValueError("event sequence must start at one")
        if timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        event_type = AgentEventType(type)
        if not request_id:
            raise ValueError("event request_id must not be empty")
        self.event_id = str(event_id or uuid4())
        self.request_id = str(request_id)
        self.step_id = str(step_id) if step_id is not None else None
        self.sequence = sequence
        self.type = event_type
        self.run_id = str(run_id)
        self.agent_version_id = str(agent_version_id)
        self.timestamp = timestamp
        self.payload = _validated_data(event_type, payload)

    @property
    def data(self) -> dict[str, Any]:
        """Compatibility alias for callers that consume the safe payload."""

        return self.payload

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "timestamp": self.timestamp.isoformat(),
            "payload": dict(self.payload),
            # These are documented AgentHub extensions.  sequence is local
            # ordering only; it is not an SSE replay guarantee.
            "sequence": self.sequence,
            "agent_version_id": self.agent_version_id,
        }


class AgentEventEmitter:
    """Concurrency-safe per-run event sequencer."""

    def __init__(
        self, *, run_id: str, agent_version_id: str, request_id: str | None = None
    ) -> None:
        self.run_id = str(run_id)
        self.agent_version_id = str(agent_version_id)
        self.request_id = str(request_id or f"event:{uuid4()}")
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def emit(
        self, event_type: AgentEventType | str, data: Mapping[str, Any]
    ) -> AgentEvent:
        async with self._lock:
            self._sequence += 1
            return AgentEvent(
                sequence=self._sequence,
                type=event_type,
                request_id=self.request_id,
                run_id=self.run_id,
                agent_version_id=self.agent_version_id,
                timestamp=datetime.now(UTC),
                payload=data,
            )


__all__ = ["AgentEvent", "AgentEventEmitter", "AgentEventType"]
