"""Provider-neutral memory contracts the runtime is allowed to depend on.

The runtime must not import SQLAlchemy models or Celery. It selects memories
through :class:`MemorySelector` and asks for extraction through
:class:`MemoryWriteQueue`; both have null-safe defaults, so a composition that
wires neither behaves exactly as the runtime behaved before memory existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

# How many memories may be injected into one run. A hard count limit on top of
# the token budget: eight short statements is already more standing context
# than most turns can act on, and a hundred would be a retrieval system rather
# than a memory.
MAX_INJECTED_MEMORIES = 8

# What the extractor is allowed to write. Below the floor it is noise ("ok"),
# above the ceiling it is a summary of the conversation rather than a fact.
MIN_MEMORY_LENGTH = 8
MAX_MEMORY_LENGTH = 400
# Per turn, not per run. A turn that claims to have learned ten new permanent
# facts has misunderstood the job.
MAX_MEMORIES_PER_TURN = 3


@dataclass(frozen=True, slots=True)
class SelectedMemory:
    """One memory as the runtime sees it: an id, a kind and a sentence."""

    id: UUID
    kind: str
    content: str
    salience: int


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """One statement the extractor proposes to remember."""

    content: str
    kind: str


class MemorySelector(Protocol):
    async def select(
        self, *, workspace_id: UUID, agent_id: UUID, query: str, limit: int
    ) -> tuple[SelectedMemory, ...]:
        """Rank the agent's active memories against the question being asked."""

    async def load(
        self, *, workspace_id: UUID, memory_ids: tuple[UUID, ...]
    ) -> tuple[SelectedMemory, ...]:
        """Re-read an earlier selection by id, preserving the given order."""


class MemoryWriteQueue(Protocol):
    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        """Ask for extraction later, elsewhere, and never on this code path."""


__all__ = [
    "MAX_INJECTED_MEMORIES",
    "MAX_MEMORIES_PER_TURN",
    "MAX_MEMORY_LENGTH",
    "MIN_MEMORY_LENGTH",
    "MemoryCandidate",
    "MemorySelector",
    "MemoryWriteQueue",
    "SelectedMemory",
]
