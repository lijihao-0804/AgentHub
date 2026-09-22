"""Provider-neutral memory contracts the runtime is allowed to depend on.

The runtime must not import SQLAlchemy models or Celery. It selects memories
through :class:`MemorySelector` and asks for extraction through
:class:`MemoryWriteQueue`; both have null-safe defaults, so a composition that
wires neither behaves exactly as the runtime behaved before memory existed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
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
MAX_EVIDENCE_LENGTH = 300
MEMORY_SNAPSHOT_INTEGRITY_ERROR = "AGENT_MEMORY_SNAPSHOT_INTEGRITY_ERROR"


class MemorySnapshotIntegrityError(RuntimeError):
    """A frozen memory snapshot cannot be reconstructed safely."""

    code = MEMORY_SNAPSHOT_INTEGRITY_ERROR


def memory_content_hash(content: str) -> str:
    """Return the canonical content hash used by persisted memory rows."""

    normalized = " ".join(content.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedMemory:
    """One memory as the runtime sees it: an id, a kind and a sentence."""

    id: UUID
    kind: str
    content: str
    salience: int
    # The empty default keeps old test-only selectors source compatible. The
    # production store always supplies the authoritative persisted hash.
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """One statement the extractor proposes to remember."""

    content: str
    kind: str
    evidence: str | None = None


class MemorySelector(Protocol):
    async def select(
        self, *, workspace_id: UUID, agent_id: UUID, query: str, limit: int
    ) -> tuple[SelectedMemory, ...]:
        """Rank the agent's active memories against the question being asked."""

    async def load(
        self,
        *,
        workspace_id: UUID,
        memory_ids: tuple[UUID, ...],
        memory_content_hashes: Mapping[UUID | str, str] | None = None,
    ) -> tuple[SelectedMemory, ...]:
        """Re-read an earlier selection by id, preserving the given order."""

    async def touch(self, *, workspace_id: UUID, memory_ids: tuple[UUID, ...]) -> None:
        """Mark only memories that reached the admitted model input as used."""


class MemoryWriteQueue(Protocol):
    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        """Ask for extraction later, elsewhere, and never on this code path."""


__all__ = [
    "MAX_INJECTED_MEMORIES",
    "MAX_MEMORIES_PER_TURN",
    "MAX_MEMORY_LENGTH",
    "MAX_EVIDENCE_LENGTH",
    "MEMORY_SNAPSHOT_INTEGRITY_ERROR",
    "MIN_MEMORY_LENGTH",
    "MemoryCandidate",
    "MemorySnapshotIntegrityError",
    "MemorySelector",
    "MemoryWriteQueue",
    "SelectedMemory",
    "memory_content_hash",
]
