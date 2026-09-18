"""Provider-neutral, safe AgentHub run events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AgentEventType(StrEnum):
    RUN_STARTED = "run.started"
    CONTEXT_BUDGET = "context.budget"
    MESSAGE_DELTA = "message.delta"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    APPROVAL_REQUIRED = "approval.required"
    USAGE = "usage"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


_ALLOWED_FIELDS: dict[AgentEventType, frozenset[str]] = {
    AgentEventType.RUN_STARTED: frozenset({"status", "model_step_count", "tool_call_count"}),
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
    AgentEventType.TOOL_STARTED: frozenset({"tool_call_id", "tool_identity"}),
    AgentEventType.TOOL_COMPLETED: frozenset(
        {"tool_call_id", "tool_identity", "status", "error_code", "duration_ms"}
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
    AgentEventType.USAGE: frozenset(
        {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
    ),
    AgentEventType.RUN_COMPLETED: frozenset(
        {"status", "model_step_count", "tool_call_count", "output"}
    ),
    AgentEventType.RUN_FAILED: frozenset(
        {"status", "failure_code", "model_step_count", "tool_call_count"}
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

    __slots__ = ("sequence", "type", "run_id", "agent_version_id", "timestamp", "data")

    def __init__(
        self,
        *,
        sequence: int,
        type: AgentEventType | str,
        run_id: str,
        agent_version_id: str,
        timestamp: datetime,
        data: Mapping[str, Any],
    ) -> None:
        if sequence < 1:
            raise ValueError("event sequence must start at one")
        if timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        event_type = AgentEventType(type)
        self.sequence = sequence
        self.type = event_type
        self.run_id = str(run_id)
        self.agent_version_id = str(agent_version_id)
        self.timestamp = timestamp
        self.data = _validated_data(event_type, data)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.type.value,
            "run_id": self.run_id,
            "agent_version_id": self.agent_version_id,
            "timestamp": self.timestamp.isoformat(),
            "data": dict(self.data),
        }


class AgentEventEmitter:
    """Concurrency-safe per-run event sequencer."""

    def __init__(self, *, run_id: str, agent_version_id: str) -> None:
        self.run_id = str(run_id)
        self.agent_version_id = str(agent_version_id)
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
                run_id=self.run_id,
                agent_version_id=self.agent_version_id,
                timestamp=datetime.now(UTC),
                data=data,
            )


__all__ = ["AgentEvent", "AgentEventEmitter", "AgentEventType"]
