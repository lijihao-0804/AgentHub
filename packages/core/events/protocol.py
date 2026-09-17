from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

EventType = Literal[
    "run.started",
    "message.delta",
    "tool.proposed",
    "tool.completed",
    "run.waiting_approval",
    "run.completed",
    "run.failed",
]


class AgentHubEvent(Protocol):
    event_type: EventType
    request_id: str
    run_id: str | None
    sequence: int
    created_at: datetime
    data: dict[str, Any]
