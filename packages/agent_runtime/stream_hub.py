"""Decouple a run's lifetime from the connection that started it.

Before this module a run *was* its HTTP request. ``stream()`` produced the SSE
body directly, so cancelling that generator -- which is what a client
disconnect does -- ran ``abort_if_active()`` and killed the run. Closing a tab
killed a run that might be mid-WRITE.

That behaviour was not an accident: aborting is what kept the system free of
zombie ``RUNNING`` rows, and that property is worth keeping. So the fix is not
to stop aborting, it is to stop treating *one* consumer's disconnect as the end
of the run:

* a run is owned by a :class:`RunStreamHub`, not by a consumer;
* any number of consumers may subscribe and leave;
* when the last one leaves, a grace period starts. If nobody re-attaches
  before it expires, the run is aborted exactly as before.

So "nobody is watching any more" still terminates the run, but a dropped
connection, a page reload, or a hand-off to another tab no longer does.

The events are also written to ``agent_run_events`` as they pass through, which
is what lets a re-attaching consumer replay what it missed instead of starting
blind -- and what lets a consumer in another process follow a run it did not
start at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from uuid import UUID

from packages.agent_runtime.event_store import RunEventRecorder
from packages.agent_runtime.events import AgentEvent

logger = logging.getLogger(__name__)

# A subscriber that falls this far behind is not keeping up with a live stream
# and is better served by reconnecting and replaying from the durable log.
_SUBSCRIBER_QUEUE_MAXSIZE = 512


class RunStreamHub:
    """One live run, plus whoever is currently watching it."""

    def __init__(
        self,
        *,
        run_id: UUID,
        source: asyncio.Queue[AgentEvent | None],
        abort: Callable[[], Awaitable[None]],
        grace_seconds: float | None,
        recorder: RunEventRecorder | None = None,
        on_closed: Callable[[UUID], None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._source = source
        self._abort = abort
        # ``None`` disables the unwatched-run abort entirely, for an owner that
        # executes rather than watches.
        self._grace_seconds = grace_seconds
        self._recorder = recorder
        self._on_closed = on_closed
        self._subscribers: set[asyncio.Queue[AgentEvent | None]] = set()
        self._grace_task: asyncio.Task[None] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._closed = False
        self._last_sequence = 0

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def start(self) -> None:
        if self._pump is None:
            if self._recorder is not None:
                self._recorder.start()
            self._pump = asyncio.create_task(self._run_pump())

    async def _run_pump(self) -> None:
        """Move events from the producer to the log and to every subscriber."""

        try:
            while True:
                event = await self._source.get()
                if event is None:
                    break
                self._last_sequence = event.sequence
                if self._recorder is not None:
                    self._recorder.record(event)
                self._broadcast(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("agent_run_stream_pump_failed", exc_info=True)
        finally:
            await self._finish()

    def _broadcast(self, event: AgentEvent | None) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The durable log is the safety net: drop the slow subscriber
                # rather than let it apply backpressure to a live run.
                logger.warning(
                    "agent_run_stream_subscriber_dropped",
                    extra={"run_id": str(self._run_id)},
                )
                self._subscribers.discard(queue)

    async def _finish(self) -> None:
        self._closed = True
        self._cancel_grace()
        self._broadcast(None)
        self._subscribers.clear()
        if self._recorder is not None:
            await self._recorder.aclose()
        if self._on_closed is not None:
            self._on_closed(self._run_id)

    async def subscribe(self) -> AsyncIterator[AgentEvent]:
        """Watch the live run. Leaving does not end it; being last to leave might."""

        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_MAXSIZE
        )
        if self._closed:
            return
        self._subscribers.add(queue)
        self._cancel_grace()
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            self._subscribers.discard(queue)
            self._start_grace_if_idle()

    def _cancel_grace(self) -> None:
        task = self._grace_task
        self._grace_task = None
        if task is not None and not task.done():
            task.cancel()

    def _start_grace_if_idle(self) -> None:
        if self._grace_seconds is None:
            # The worker owns its runs outright: nobody is expected to be
            # watching, so "unwatched" must not mean "abandoned".
            return
        if self._closed or self._subscribers or self._grace_task is not None:
            return
        self._grace_task = asyncio.create_task(self._grace())

    async def wait_closed(self) -> None:
        """Block until the run has finished publishing.

        Used by an owner that is executing the run rather than watching it --
        the worker, which must keep the process alive until the run ends.
        """

        pump = self._pump
        if pump is not None:
            await asyncio.gather(pump, return_exceptions=True)

    async def _grace(self) -> None:
        """Abort only if the run is still unwatched when the window closes."""

        try:
            await asyncio.sleep(self._grace_seconds)
        except asyncio.CancelledError:
            return
        self._grace_task = None
        if self._closed or self._subscribers:
            return
        logger.info(
            "agent_run_stream_grace_expired",
            extra={"run_id": str(self._run_id)},
        )
        await self._abort()

    async def aclose(self) -> None:
        """Stop the hub without waiting for the grace window (shutdown path)."""

        self._cancel_grace()
        pump = self._pump
        self._pump = None
        if pump is not None and not pump.done():
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if not self._closed:
            await self._finish()

    async def abort_if_unwatched(self) -> None:
        """Abort and fully drain the producer when no subscriber remains.

        Direct ``AgentRunService.stream`` consumers own their subscription and
        must get deterministic cancellation on ``aclose``. Reconnecting
        consumers use ``subscribe`` directly and retain the grace window.
        """

        self._cancel_grace()
        if self._closed or self._subscribers:
            return
        await self._abort()
        await self.wait_closed()


class RunStreamRegistry:
    """The live runs this process is executing, addressable by run id."""

    def __init__(self) -> None:
        self._hubs: dict[UUID, RunStreamHub] = {}

    def register(self, hub: RunStreamHub) -> None:
        self._hubs[hub.run_id] = hub

    def get(self, run_id: UUID) -> RunStreamHub | None:
        hub = self._hubs.get(run_id)
        if hub is not None and hub.closed:
            self._hubs.pop(run_id, None)
            return None
        return hub

    def discard(self, run_id: UUID) -> None:
        self._hubs.pop(run_id, None)

    def __len__(self) -> int:
        return len(self._hubs)

    async def aclose(self) -> None:
        hubs = list(self._hubs.values())
        self._hubs.clear()
        for hub in hubs:
            with suppress(Exception):
                await hub.aclose()


__all__ = ["RunStreamHub", "RunStreamRegistry"]
