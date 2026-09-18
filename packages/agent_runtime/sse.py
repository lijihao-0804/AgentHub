"""SSE serialization for AgentHub events."""

from __future__ import annotations

import json

from packages.agent_runtime.events import AgentEvent


def event_to_sse(event: AgentEvent) -> str:
    payload = json.dumps(
        event.as_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return f"event: {event.type.value}\ndata: {payload}\n\n"


__all__ = ["event_to_sse"]
