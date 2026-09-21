"""The two seams the work layer plugs into the runtime through.

Threads and artifacts sit *above* the runtime: they read agent runs, so they
depend on this package and this package must not depend on them. The runtime
therefore declares what it needs as protocols here, and composition wires the
real implementations in. Without a provider the runtime behaves exactly as it
did before threads existed, which is also what keeps Playground runs unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ThreadTurnContext:
    """One completed earlier turn, as the next turn should see it."""

    user_input: str
    final_output: str
    # One line per artifact, never the artifact itself: twenty abstracts would
    # eat the budget to tell the model something one line already tells it.
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ThreadConversation:
    turns: tuple[ThreadTurnContext, ...] = ()
    # What existed before the "most recent N" cut, so the drop is reportable
    # rather than invisible.
    turns_available: int = 0


class ThreadContextProvider(Protocol):
    """Supplies the earlier conversation a threaded run should start from."""

    async def conversation(
        self, *, workspace_id: UUID, thread_id: UUID, before_run_id: UUID, max_turns: int
    ) -> ThreadConversation: ...


@dataclass(frozen=True, slots=True)
class RecordedToolCall:
    """One completed tool call, offered to the work layer to keep or ignore."""

    tool_identity: str
    tool_call_id: str
    step_sequence: int
    arguments: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


class RunArtifactRecorder(Protocol):
    """Turns tool results into artifacts, if it recognizes any of them.

    The model never writes an artifact. It decides what to search for; what
    came back is copied from the tool result by this recorder, so a fabricated
    paper has no path into the record.
    """

    async def record(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        created_by: UUID,
        calls: tuple[RecordedToolCall, ...],
    ) -> None: ...


__all__ = [
    "RecordedToolCall",
    "RunArtifactRecorder",
    "ThreadContextProvider",
    "ThreadConversation",
    "ThreadTurnContext",
]
