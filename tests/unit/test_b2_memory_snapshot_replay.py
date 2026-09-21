"""B2: a run is given its memories once, and keeps them.

Memory is mutable and runs are replayable, and those two facts fight. A run
that pauses for an approval and resumes an hour later, a run replayed to
explain itself, an evaluation comparing memory-on against memory-off -- all of
them re-enter PREPARE, and if PREPARE re-queried, each would silently see a
different belief set than the one that produced the answer being explained.

So the first PREPARE freezes the chosen ids into the run row, and every later
PREPARE *loads those ids* -- including ones that have since been superseded or
invalidated, because the question a replay answers is "what was this run
given", not "what would it be given now". ADR-011.

These exercise ``_AgentRunGraph._memories`` against a selector that records
which of its two methods was called, which is the only thing that
distinguishes the two branches from the outside.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.context_budget import ContextCategory
from packages.agent_runtime.runtime import _AgentRunGraph, _categorize_messages
from packages.memory.contracts import SelectedMemory
from packages.model_gateway.contracts import ModelMessage


class _RecordingSelector:
    """Answers both Protocol methods and remembers which one was asked."""

    def __init__(self, memories: tuple[SelectedMemory, ...]) -> None:
        self._memories = memories
        self.select_calls = 0
        self.load_calls: list[tuple[UUID, ...]] = []

    async def select(self, *, workspace_id, agent_id, query, limit):  # noqa: ANN001
        self.select_calls += 1
        return self._memories[:limit]

    async def load(self, *, workspace_id, memory_ids):  # noqa: ANN001
        self.load_calls.append(tuple(memory_ids))
        by_id = {item.id: item for item in self._memories}
        return tuple(by_id[mid] for mid in memory_ids if mid in by_id)


class _FakeSession:
    """Just enough to let the snapshot UPDATE run without a database."""

    def __init__(self, statements: list[object]) -> None:
        self._statements = statements

    async def execute(self, statement):  # noqa: ANN001
        self._statements.append(statement)

    async def commit(self) -> None:
        return None


def _session_factory(statements: list[object]):
    @asynccontextmanager
    async def factory():
        yield _FakeSession(statements)

    return factory


def _spec(*, long_term_memory: bool) -> SimpleNamespace:
    """Stands in for a FrozenAgentSpec: `_memories` reads `runtime` and nothing else."""

    return SimpleNamespace(runtime={"memory": {"long_term_memory": long_term_memory}})


def _graph(selector, *, snapshot: dict | None = None, statements: list | None = None):
    service = SimpleNamespace(
        memory_selector=selector,
        session_factory=_session_factory(statements if statements is not None else []),
    )
    run = SimpleNamespace(
        id=uuid4(),
        input_text="what did we decide about Fridays?",
        effective_memory_snapshot=snapshot if snapshot is not None else {},
    )
    return _AgentRunGraph.__new__(_AgentRunGraph), service, run


async def _memories(graph_service_run, spec, *, workspace_id, agent_id):
    graph, service, run = graph_service_run
    graph.service = service
    graph.run = run
    return await _AgentRunGraph._memories(graph, spec, workspace_id=workspace_id, agent_id=agent_id)


def _some(count: int) -> tuple[SelectedMemory, ...]:
    return tuple(
        SelectedMemory(id=uuid4(), kind="FACT", content=f"durable fact {i}", salience=1)
        for i in range(count)
    )


@pytest.mark.asyncio
async def test_the_switch_being_off_costs_nothing_at_all() -> None:
    # The common path: every agent has this off until someone turns it on, and
    # an off agent must not even reach the selector.
    selector = _RecordingSelector(_some(3))
    messages, metadata = await _memories(
        _graph(selector), _spec(long_term_memory=False), workspace_id=uuid4(), agent_id=uuid4()
    )

    assert messages == []
    assert metadata is None
    assert selector.select_calls == 0
    assert selector.load_calls == []


@pytest.mark.asyncio
async def test_the_first_prepare_selects_and_writes_the_snapshot() -> None:
    selector = _RecordingSelector(_some(3))
    statements: list[object] = []
    bundle = _graph(selector, statements=statements)

    messages, metadata = await _memories(
        bundle, _spec(long_term_memory=True), workspace_id=uuid4(), agent_id=uuid4()
    )

    assert selector.select_calls == 1
    assert selector.load_calls == []
    assert metadata == {"memory_count": 3, "replayed_from_snapshot": False}
    assert len(messages) == 1
    assert statements, "the snapshot must be persisted, not only held in memory"

    snapshot = bundle[2].effective_memory_snapshot
    assert snapshot["selected_at"]
    assert len(snapshot["memory_ids"]) == 3


@pytest.mark.asyncio
async def test_a_second_prepare_replays_the_snapshot_instead_of_reselecting() -> None:
    memories = _some(3)
    selector = _RecordingSelector(memories)
    bundle = _graph(selector, statements=[])
    spec = _spec(long_term_memory=True)
    workspace_id, agent_id = uuid4(), uuid4()

    await _memories(bundle, spec, workspace_id=workspace_id, agent_id=agent_id)
    _, metadata = await _memories(bundle, spec, workspace_id=workspace_id, agent_id=agent_id)

    assert selector.select_calls == 1, "selection happens once per run, not once per PREPARE"
    assert selector.load_calls == [tuple(item.id for item in memories)]
    assert metadata == {"memory_count": 3, "replayed_from_snapshot": True}


@pytest.mark.asyncio
async def test_selecting_nothing_is_recorded_and_is_not_reselected() -> None:
    # The reason the snapshot is an object and not a bare list: an empty list
    # and "not selected yet" are both falsy, and telling them apart is the
    # whole determinism claim.
    selector = _RecordingSelector(())
    bundle = _graph(selector, statements=[])
    spec = _spec(long_term_memory=True)

    messages, first = await _memories(bundle, spec, workspace_id=uuid4(), agent_id=uuid4())
    _, second = await _memories(bundle, spec, workspace_id=uuid4(), agent_id=uuid4())

    assert messages == []
    assert first == {"memory_count": 0, "replayed_from_snapshot": False}
    assert second["replayed_from_snapshot"] is True
    assert selector.select_calls == 1


@pytest.mark.asyncio
async def test_a_memory_invalidated_after_the_run_started_is_still_replayed() -> None:
    # A human switching a memory off must not retroactively change what an
    # already-running run was given, or "why did it say that" stops being
    # answerable.
    memories = _some(2)
    selector = _RecordingSelector(memories)
    bundle = _graph(selector, statements=[])
    spec = _spec(long_term_memory=True)
    await _memories(bundle, spec, workspace_id=uuid4(), agent_id=uuid4())

    # The store would now no longer return the first one to `select`.
    selector._memories = memories[1:]

    messages, metadata = await _memories(bundle, spec, workspace_id=uuid4(), agent_id=uuid4())

    # `load` was asked for both ids: the snapshot decides, not the store's
    # current view. What comes back is whatever still exists, which is the
    # store's business -- the run's business is to ask for the frozen set.
    assert selector.load_calls[-1] == tuple(item.id for item in memories)
    assert metadata["replayed_from_snapshot"] is True
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_what_reaches_the_window_is_one_evictable_memory_message() -> None:
    # The two halves joined up: what `_memories` returns has to categorize as
    # MEMORY once PREPARE splices it in after the system prefix.
    selector = _RecordingSelector(_some(4))
    messages, _ = await _memories(
        _graph(selector, statements=[]),
        _spec(long_term_memory=True),
        workspace_id=uuid4(),
        agent_id=uuid4(),
    )
    spliced = [
        ModelMessage(role="system", content="runtime policy"),
        ModelMessage(role="system", content="agent prompt"),
        *messages,
        ModelMessage(role="user", content="the question being asked now"),
    ]

    categorized = _categorize_messages(spliced, memory_message_count=len(messages))

    assert [item.category for item in categorized] == [
        ContextCategory.RUNTIME_POLICY,
        ContextCategory.SYSTEM_PROMPT,
        ContextCategory.MEMORY,
        ContextCategory.CURRENT_USER_TASK,
    ]
    assert len(json.loads(messages[0].content)["memories"]) == 4
