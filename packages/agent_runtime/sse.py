"""SSE serialization for AgentHub events."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from packages.agent_runtime.events import AgentEvent


def event_to_sse(event: AgentEvent) -> str:
    payload = json.dumps(
        event.as_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return f"event: {event.type.value}\ndata: {payload}\n\n"


async def sse_frames(
    stream: AsyncIterator[AgentEvent], *, heartbeat_seconds: float
) -> AsyncIterator[str]:
    """Serialize an event stream as SSE, keeping the connection warm.

    Closing this generator -- which is what a client disconnect does -- closes
    the subscription, not the run. Whether the run then survives is the stream
    hub's decision, and it only aborts once nobody has come back for it.

    It lives here rather than in a route because every stream endpoint needs
    exactly this behaviour, and a second copy of it is a second place for the
    heartbeat and the cancellation handling to drift apart.
    """

    iterator = stream.__aiter__()
    pending = asyncio.create_task(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            yield event_to_sse(event)
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


__all__ = ["event_to_sse", "sse_frames"]
