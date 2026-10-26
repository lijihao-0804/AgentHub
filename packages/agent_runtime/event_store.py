"""Durable storage and replay for the run event stream.

A run used to exist only for as long as the HTTP request that started it. The
events it published went straight to the socket and were never stored, so a
consumer that dropped the connection could not come back, a second consumer
could not watch, and a consumer in another process could not follow at all.

Persisting the stream fixes all three with one mechanism, because the events
already carry everything replay needs: a monotonic ``sequence`` per run and a
payload that the event envelope has already validated against a field
whitelist. Nothing provider-specific and nothing secret can reach this module;
it stores what went out over the wire, verbatim.

Two deliberate limits:

* ``message.delta`` is not stored. The log would otherwise be O(tokens) and buy
  back only the typing animation. Replay therefore reconstructs the run's
  structure and its final output, not its keystrokes, and sequence gaps are
  normal.
* Structural events persist before broadcast. The legacy nonblocking recorder
  exposes incompleteness on queue/write failure; it never claims a full log.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.events import AgentEvent, AgentEventType
from packages.agent_runtime.models import AgentRun, AgentRunEvent
from packages.core.errors.exceptions import AgentHubError

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]

# Persisting every token delta would make the event log grow with the model's
# output length to buy back an animation that a reconnecting client does not
# need: the completed message and the final output are both stored.
NON_PERSISTED_EVENT_TYPES: frozenset[AgentEventType] = frozenset({AgentEventType.MESSAGE_DELTA})

_QUEUE_MAXSIZE = 2048


class EventSequenceConflict(ValueError):
    """A replay identity disagrees with the already persisted frame."""


def should_persist(event: AgentEvent) -> bool:
    return event.type not in NON_PERSISTED_EVENT_TYPES


def _row_values(event: AgentEvent, *, workspace_id: UUID, run_id: UUID) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "agent_run_id": run_id,
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.type.value,
        "request_id": event.request_id,
        "step_id": event.step_id,
        "payload": dict(event.payload),
        "occurred_at": event.timestamp,
    }


class RunEventRecorder:
    """Persists structural events before publication; supports legacy queues."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        workspace_id: UUID,
        run_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._run_id = run_id
        self._queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._writer: asyncio.Task[None] | None = None
        self._failed = False

    async def arecord(self, event: AgentEvent) -> None:
        """Persist structural events before they become visible to followers."""
        if not should_persist(event):
            return
        try:
            await self._flush([event], strict=True)
        except Exception:
            self._failed = True
            raise

    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._drain())

    def record(self, event: AgentEvent) -> None:
        """Queue an event for persistence. Never blocks, never raises."""

        if self._writer is None or not should_persist(event):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._failed = True
            # Dropping a replay frame is recoverable; stalling a run that is
            # calling real systems is not.
            logger.warning(
                "agent_run_event_dropped",
                extra={"run_id": str(self._run_id), "sequence": event.sequence},
            )

    async def aclose(self) -> None:
        """Flush everything already queued, then stop the writer."""

        writer = self._writer
        if writer is None:
            await self._mark_completeness()
            return
        self._writer = None
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        # The run is finishing either way; a writer that cannot finish must not
        # hold the response open.
        done, _ = await asyncio.wait({writer}, timeout=10)
        if not done:
            self._failed = True
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await self._mark_completeness()

    async def _mark_completeness(self):
        try:
            async with self._session_factory() as session:
                latest_type = (
                    select(AgentRunEvent.event_type)
                    .where(
                        AgentRunEvent.workspace_id == self._workspace_id,
                        AgentRunEvent.agent_run_id == self._run_id,
                    )
                    .order_by(AgentRunEvent.sequence.desc())
                    .limit(1)
                    .scalar_subquery()
                )
                completed_segment = or_(
                    and_(AgentRun.status == "WAITING_APPROVAL", latest_type == "approval.required"),
                    and_(AgentRun.status == "SUCCEEDED", latest_type == "run.completed"),
                    and_(AgentRun.status == "CANCELLED", latest_type == "run.cancelled"),
                    and_(
                        AgentRun.status.in_({"FAILED", "NEEDS_ATTENTION"}),
                        latest_type == "run.failed",
                    ),
                )
                await session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.workspace_id == self._workspace_id,
                        AgentRun.id == self._run_id,
                        *([] if self._failed else [AgentRun.event_log_failed.is_(False)]),
                        # An older stream's close must not mark a resumed,
                        # still-running execution as fully recorded.
                        *([] if self._failed else [completed_segment]),
                    )
                    .values(
                        **(
                            {"event_log_complete": False, "event_log_failed": True}
                            if self._failed
                            else {"event_log_complete": True}
                        )
                    )
                )
                await session.commit()
        except Exception:
            self._failed = True
            logger.warning(
                "agent_run_event_completeness_unknown", extra={"run_id": str(self._run_id)}
            )

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            batch = [event]
            # Whatever else is already queued belongs in the same round trip.
            while not self._queue.empty():
                nxt = self._queue.get_nowait()
                if nxt is None:
                    await self._flush(batch)
                    return
                batch.append(nxt)
            await self._flush(batch)

    async def _flush(self, batch: Sequence[AgentEvent], *, strict=False) -> None:
        if not batch:
            return
        rows = [
            _row_values(event, workspace_id=self._workspace_id, run_id=self._run_id)
            for event in batch
        ]
        try:
            async with self._session_factory() as session:
                inserted = await session.execute(
                    insert(AgentRunEvent)
                    .on_conflict_do_nothing()
                    .returning(AgentRunEvent.sequence, AgentRunEvent.event_id),
                    rows,
                )
                if len(inserted.all()) != len(rows):
                    existing = dict(
                        (
                            await session.execute(
                                select(AgentRunEvent.sequence, AgentRunEvent.event_id).where(
                                    AgentRunEvent.workspace_id == self._workspace_id,
                                    AgentRunEvent.agent_run_id == self._run_id,
                                    AgentRunEvent.sequence.in_([event.sequence for event in batch]),
                                )
                            )
                        ).all()
                    )
                    if any(existing.get(event.sequence) != event.event_id for event in batch):
                        raise EventSequenceConflict("Conflicting durable run event sequence")
                await session.commit()
        except Exception as error:
            self._failed = True
            # Persisting the stream is a durability feature, not a correctness
            # one: the run's own outcome is written by _complete_run.
            logger.warning(
                "agent_run_event_persist_failed",
                # SQLAlchemy exceptions may include the event's raw output in
                # bound parameters. Keep diagnostics strictly metadata-only.
                extra={
                    "run_id": str(self._run_id),
                    "count": len(rows),
                    "error_type": type(error).__name__,
                },
            )
            if strict:
                if isinstance(error, EventSequenceConflict):
                    raise
                raise AgentHubError(
                    "AGENT_EVENT_LOG_UNAVAILABLE",
                    "The run event could not be durably recorded.",
                    503,
                ) from None


class RunEventReader:
    """Reads a run's stored events back as the same envelopes that were sent."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def latest_sequence(self, *, workspace_id, run_id):
        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.max(AgentRunEvent.sequence)).where(
                        AgentRunEvent.workspace_id == workspace_id,
                        AgentRunEvent.agent_run_id == run_id,
                    )
                )
                or 0
            )

    async def agent_version_id(self, *, workspace_id: UUID, run_id: UUID) -> UUID | None:
        """The run's agent version, needed to rebuild event envelopes.

        Stored once on the run rather than copied onto every event row: it is
        constant for the life of a run, and a per-row copy would be a second
        source of truth for the same fact.
        """

        async with self._session_factory() as session:
            return await session.scalar(
                select(AgentRun.agent_version_id).where(
                    AgentRun.workspace_id == workspace_id, AgentRun.id == run_id
                )
            )

    async def read_after(
        self,
        *,
        workspace_id: UUID,
        run_id: UUID,
        agent_version_id: UUID,
        after_sequence: int,
        limit: int = 500,
    ) -> list[AgentEvent]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(AgentRunEvent)
                    .where(
                        AgentRunEvent.workspace_id == workspace_id,
                        AgentRunEvent.agent_run_id == run_id,
                        AgentRunEvent.sequence > after_sequence,
                    )
                    .order_by(AgentRunEvent.sequence)
                    .limit(limit)
                )
            ).all()
        return [_event_from_row(row, agent_version_id=agent_version_id) for row in rows]


def _event_from_row(row: AgentRunEvent, *, agent_version_id: UUID) -> AgentEvent:
    timestamp: datetime = row.occurred_at
    if timestamp.tzinfo is None:
        # The column is timestamptz, but a driver that hands back a naive value
        # must not be allowed to fail the envelope's tz-aware invariant: the
        # value it stored was UTC.
        timestamp = timestamp.replace(tzinfo=UTC)
    return AgentEvent(
        sequence=row.sequence,
        type=row.event_type,
        request_id=row.request_id,
        run_id=str(row.agent_run_id),
        agent_version_id=str(agent_version_id),
        step_id=row.step_id,
        event_id=row.event_id,
        timestamp=timestamp,
        payload=dict(row.payload or {}),
    )


__all__ = [
    "NON_PERSISTED_EVENT_TYPES",
    "RunEventReader",
    "RunEventRecorder",
    "should_persist",
]
