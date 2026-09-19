from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

EventType = Literal[
    "run.started",
    "message.started",
    "context.budget",
    "message.delta",
    "message.completed",
    "retrieval.started",
    "retrieval.completed",
    "rerank.completed",
    "tool.requested",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "approval.required",
    "approval.resolved",
    "run.cancel_requested",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "usage",
]


class AgentHubEvent(Protocol):
    event_type: EventType
    event_id: str
    request_id: str
    run_id: str
    step_id: str | None
    timestamp: datetime
    payload: dict[str, Any]
