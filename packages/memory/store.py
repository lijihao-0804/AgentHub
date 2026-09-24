"""Reading and writing long-term memory.

Selection is deliberately dumb: term overlap, then salience, then recency, over
an agent's few dozen active rows. There is no embedding index here and there is
no plan for one until the row count makes scanning expensive, because an
unexplainable recall is worse than a slow one -- see
``docs/adr/ADR-011-memory-must-be-snapshotted.md``.

Writing is where the care is. Everything in this module's write path treats the
incoming statement as untrusted text produced by a model: it is length-checked,
kind-checked, hashed, deduplicated, and capped per turn before it is allowed to
become something a future run will read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.text import search_terms
from packages.memory.contracts import (
    MAX_EVIDENCE_LENGTH,
    MAX_MEMORIES_PER_TURN,
    MAX_MEMORY_LENGTH,
    MIN_MEMORY_LENGTH,
    MemoryCandidate,
    MemorySnapshotIntegrityError,
    SelectedMemory,
    memory_content_hash,
)
from packages.memory.models import MEMORY_KINDS, WorkspaceMemory

ACTIVE = "ACTIVE"
SUPERSEDED = "SUPERSEDED"
INVALIDATED = "INVALIDATED"

# Ranking weights. A term hit is worth more than a reinforcement, because a
# memory that is about the current question beats a memory that is merely
# popular; salience only breaks ties among equally relevant rows.
_TERM_WEIGHT = 100


def content_hash(content: str) -> str:
    """Identity of a statement, insensitive to whitespace and letter case.

    Two extractions of the same fact rarely agree on punctuation. Hashing the
    normalized form is what turns "the team prefers short answers" arriving
    twice into one row with salience 2 rather than two rows that will both be
    injected and waste the budget saying the same thing.
    """

    return memory_content_hash(content)


def _selected(row: WorkspaceMemory) -> SelectedMemory:
    return SelectedMemory(
        id=row.id,
        kind=row.kind,
        content=row.content,
        salience=row.salience,
        content_hash=row.content_hash,
    )


def normalize_candidate(candidate: MemoryCandidate) -> MemoryCandidate | None:
    """Apply the write gate to one proposed statement, or refuse it.

    Returning ``None`` rather than raising is intentional: the extractor is a
    model, a model will occasionally propose rubbish, and one bad candidate in
    a batch of three must not cost the other two.
    """

    content = " ".join((candidate.content or "").split())
    if not MIN_MEMORY_LENGTH <= len(content) <= MAX_MEMORY_LENGTH:
        return None
    kind = (candidate.kind or "FACT").strip().upper()
    if kind not in MEMORY_KINDS:
        return None
    if candidate.evidence is not None and (
        not isinstance(candidate.evidence, str)
        or not candidate.evidence
        or len(candidate.evidence) > MAX_EVIDENCE_LENGTH
    ):
        return None
    return MemoryCandidate(content=content, kind=kind, evidence=candidate.evidence)


class SqlAlchemyMemoryStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    # -- read ------------------------------------------------------------

    async def select(
        self, *, workspace_id: UUID, agent_id: UUID, query: str, limit: int
    ) -> tuple[SelectedMemory, ...]:
        if limit <= 0:
            return ()
        terms = search_terms(query)
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(WorkspaceMemory)
                    .where(
                        WorkspaceMemory.workspace_id == workspace_id,
                        WorkspaceMemory.agent_id == agent_id,
                        WorkspaceMemory.status == ACTIVE,
                        or_(
                            WorkspaceMemory.expires_at.is_(None),
                            WorkspaceMemory.expires_at > now,
                        ),
                    )
                    .order_by(WorkspaceMemory.created_at.desc())
                )
            ).all()
        scored = sorted(
            rows,
            key=lambda row: (
                -_TERM_WEIGHT * _matches(row.content, terms),
                -row.salience,
                -row.created_at.timestamp(),
            ),
        )
        return tuple(_selected(row) for row in scored[:limit])

    async def load(
        self,
        *,
        workspace_id: UUID,
        memory_ids: tuple[UUID, ...],
        memory_content_hashes: dict[UUID | str, str] | None = None,
    ) -> tuple[SelectedMemory, ...]:
        """Re-read a frozen selection, in the order it was frozen.

        A run replaying its own snapshot must see what it saw, including rows
        that have since been superseded or invalidated: the snapshot records
        what the model was told, and rewriting history to be tidier would make
        the run log a worse answer to "why did it say that" than it is now.
        """

        if not memory_ids:
            if memory_content_hashes:
                raise MemorySnapshotIntegrityError(
                    "The frozen memory snapshot contains unexpected content hashes."
                )
            return ()
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(WorkspaceMemory).where(
                        WorkspaceMemory.workspace_id == workspace_id,
                        WorkspaceMemory.id.in_(memory_ids),
                    )
                )
            ).all()
        by_id = {row.id: row for row in rows}
        if memory_content_hashes is None:
            # ID-only snapshots predate hash freezing and remain replayable.
            return tuple(_selected(by_id[key]) for key in memory_ids if key in by_id)
        if len(set(memory_ids)) != len(memory_ids) or len(by_id) != len(set(memory_ids)):
            raise MemorySnapshotIntegrityError(
                "The frozen memory snapshot references a missing or duplicate memory."
            )
        expected_keys = {str(key) for key in memory_content_hashes}
        if expected_keys != {str(key) for key in memory_ids}:
            raise MemorySnapshotIntegrityError(
                "The frozen memory snapshot hash set does not match its memory ids."
            )
        selected: list[SelectedMemory] = []
        for key in memory_ids:
            row = by_id.get(key)
            expected = memory_content_hashes.get(key) or memory_content_hashes.get(str(key))
            if row is None or not isinstance(expected, str) or row.content_hash != expected:
                raise MemorySnapshotIntegrityError(
                    "The frozen memory snapshot does not match the stored memory."
                )
            selected.append(_selected(row))
        return tuple(selected)

    async def list_for_agent(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[tuple[WorkspaceMemory, ...], int]:
        statement = select(WorkspaceMemory).where(
            WorkspaceMemory.workspace_id == workspace_id,
            WorkspaceMemory.agent_id == agent_id,
        )
        counter = select(func.count()).select_from(WorkspaceMemory).where(
            WorkspaceMemory.workspace_id == workspace_id,
            WorkspaceMemory.agent_id == agent_id,
        )
        if status is not None:
            statement = statement.where(WorkspaceMemory.status == status)
            counter = counter.where(WorkspaceMemory.status == status)
        async with self.session_factory() as session:
            total = int(await session.scalar(counter) or 0)
            rows = (
                await session.scalars(
                    statement.order_by(
                        WorkspaceMemory.status,
                        WorkspaceMemory.salience.desc(),
                        WorkspaceMemory.created_at.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            for row in rows:
                session.expunge(row)
        return tuple(rows), total

    async def get(self, *, workspace_id: UUID, memory_id: UUID) -> WorkspaceMemory | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(WorkspaceMemory).where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.id == memory_id,
                )
            )
            if row is not None:
                session.expunge(row)
        return row

    # -- write -----------------------------------------------------------

    async def touch(self, *, workspace_id: UUID, memory_ids: tuple[UUID, ...]) -> None:
        """Record that these rows were actually injected somewhere.

        Separate from selection so that a read is a read. ``last_used_at`` is
        what makes decay defensible later: a memory nobody has needed in months
        is a different thing from a memory that was written months ago.
        """

        if not memory_ids:
            return
        async with self.session_factory() as session:
            await session.execute(
                update(WorkspaceMemory)
                .where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.id.in_(memory_ids),
                )
                .values(last_used_at=datetime.now(UTC))
            )
            await session.commit()

    async def record(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID,
        thread_id: UUID | None,
        source_run_id: UUID | None,
        candidates: tuple[MemoryCandidate, ...],
    ) -> tuple[UUID, ...]:
        """Write the surviving candidates and return what was created.

        Reinforcement, not duplication: a candidate whose hash already exists
        and is ACTIVE bumps that row's salience instead of inserting a second
        copy, so repeating yourself makes the agent surer rather than noisier.
        """

        accepted: list[MemoryCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = normalize_candidate(candidate)
            if normalized is None:
                continue
            digest = content_hash(normalized.content)
            if digest in seen:
                continue
            seen.add(digest)
            accepted.append(normalized)
            if len(accepted) >= MAX_MEMORIES_PER_TURN:
                break
        if not accepted:
            return ()

        created: list[UUID] = []
        async with self.session_factory() as session:
            existing = {
                row.content_hash: row
                for row in (
                    await session.scalars(
                        select(WorkspaceMemory).where(
                            WorkspaceMemory.workspace_id == workspace_id,
                            WorkspaceMemory.agent_id == agent_id,
                            WorkspaceMemory.status == ACTIVE,
                            WorkspaceMemory.content_hash.in_(
                                [content_hash(item.content) for item in accepted]
                            ),
                        )
                    )
                ).all()
            }
            for candidate in accepted:
                digest = content_hash(candidate.content)
                previous = existing.get(digest)
                if previous is not None:
                    previous.salience += 1
                    continue
                row = WorkspaceMemory(
                    # Assigned here rather than left to the column default,
                    # which SQLAlchemy only applies at flush: reading `row.id`
                    # straight after `add` would otherwise hand every caller a
                    # tuple of None.
                    id=uuid4(),
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    thread_id=thread_id,
                    source_run_id=source_run_id,
                    content=candidate.content,
                    content_hash=digest,
                    kind=candidate.kind,
                    status=ACTIVE,
                    provenance={
                        **(
                            {"extracted_from_run": str(source_run_id)}
                            if source_run_id is not None
                            else {}
                        ),
                        **(
                            {"evidence": candidate.evidence}
                            if candidate.evidence is not None
                            else {}
                        ),
                    },
                )
                try:
                    async with session.begin_nested():
                        session.add(row)
                        await session.flush()
                except IntegrityError:
                    # Only this candidate loses its savepoint. Another writer
                    # won the partial unique active-hash race; reinforce that
                    # row while allowing siblings in this batch to commit.
                    previous = await session.scalar(
                        select(WorkspaceMemory).where(
                            WorkspaceMemory.workspace_id == workspace_id,
                            WorkspaceMemory.agent_id == agent_id,
                            WorkspaceMemory.status == ACTIVE,
                            WorkspaceMemory.content_hash == digest,
                        )
                    )
                    if previous is not None:
                        previous.salience += 1
                        continue
                    raise
                created.append(row.id)
            await session.commit()
        return tuple(created)

    async def supersede(
        self, *, workspace_id: UUID, memory_id: UUID, superseded_by_id: UUID
    ) -> bool:
        """Reserved internal transition; automatic extraction never calls it."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(WorkspaceMemory)
                .where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.id == memory_id,
                    WorkspaceMemory.status == ACTIVE,
                )
                .values(status=SUPERSEDED, superseded_by_id=superseded_by_id)
            )
            await session.commit()
        return bool(result.rowcount)

    async def invalidate(self, *, workspace_id: UUID, memory_id: UUID) -> bool:
        """Human override. Never a DELETE -- see the module docstring."""

        async with self.session_factory() as session:
            result = await session.execute(
                update(WorkspaceMemory)
                .where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.id == memory_id,
                    WorkspaceMemory.status != INVALIDATED,
                )
                .values(status=INVALIDATED)
            )
            await session.commit()
        return bool(result.rowcount)

    async def reactivate(self, *, workspace_id: UUID, memory_id: UUID) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                update(WorkspaceMemory)
                .where(
                    WorkspaceMemory.workspace_id == workspace_id,
                    WorkspaceMemory.id == memory_id,
                    WorkspaceMemory.status == INVALIDATED,
                )
                .values(status=ACTIVE, superseded_by_id=None)
            )
            await session.commit()
        return bool(result.rowcount)


def _matches(content: str, terms: list[str]) -> int:
    if not terms:
        return 0
    haystack = content.casefold()
    return sum(1 for term in terms if term in haystack)


__all__ = [
    "ACTIVE",
    "INVALIDATED",
    "SUPERSEDED",
    "SqlAlchemyMemoryStore",
    "content_hash",
    "normalize_candidate",
]
