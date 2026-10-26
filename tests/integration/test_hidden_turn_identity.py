"""Real PostgreSQL coverage for stable Thread/Turn request identity."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime_service import AgentRunService
from packages.core.errors.exceptions import AgentHubError
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.service import ThreadService
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import (
    db_factory as db_factory,
)
from tests.integration.test_m5a_approval_runtime import (
    migrated_database as migrated_database,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]


async def _thread(factory):
    async with factory() as session:
        base = await _seed(session, label=uuid4().hex)
        thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title="stable turn identity",
            created_by=base["user"].id,
        )
        session.add(thread)
        await session.commit()
    return base, thread


@pytest.mark.asyncio
async def test_same_body_with_new_token_is_a_new_deliberate_question(db_factory) -> None:
    base, thread = await _thread(db_factory)
    service = ThreadService(db_factory)

    first = await service.open_turn(
        base["context"], thread.id, user_input="what changed?", client_token=uuid4().hex
    )
    second = await service.open_turn(
        base["context"], thread.id, user_input="what changed?", client_token=uuid4().hex
    )

    assert first.id != second.id
    assert (first.sequence, second.sequence) == (1, 2)
    async with db_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ThreadTurn)
                .where(ThreadTurn.thread_id == thread.id)
            )
            == 2
        )


@pytest.mark.asyncio
async def test_same_token_and_normalized_body_replays_one_turn(db_factory) -> None:
    base, thread = await _thread(db_factory)
    service = ThreadService(db_factory)
    token = uuid4().hex

    first = await service.open_turn(
        base["context"], thread.id, user_input="  deploy status  ", client_token=token
    )
    replay = await service.open_turn(
        base["context"], thread.id, user_input="deploy status", client_token=token
    )
    matched = await service.token_turn(
        base["context"], thread.id, token, user_input="deploy status"
    )

    assert first.id == replay.id == matched.id
    assert first.input_hash == matched.input_hash


@pytest.mark.asyncio
async def test_same_token_with_changed_body_is_an_idempotency_conflict(db_factory) -> None:
    base, thread = await _thread(db_factory)
    service = ThreadService(db_factory)
    token = uuid4().hex
    await service.open_turn(base["context"], thread.id, user_input="answer A", client_token=token)

    for call in (
        service.open_turn(base["context"], thread.id, user_input="answer B", client_token=token),
        service.token_turn(base["context"], thread.id, token, user_input="answer B"),
    ):
        with pytest.raises(AgentHubError) as raised:
            await call
        assert raised.value.code == "THREAD_TURN_IDEMPOTENCY_CONFLICT"
        assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_prepare_creates_one_run_and_one_stable_binding(db_factory) -> None:
    base, thread = await _thread(db_factory)
    service = ThreadService(db_factory, run_service=AgentRunService(db_factory))
    token = uuid4().hex

    async def prepare():
        turn = await service.open_turn(
            base["context"], thread.id, user_input="same request", client_token=token
        )
        return await service.ensure_stream_run(
            base["context"],
            thread_id=thread.id,
            agent_version_id=base["version"].id,
            turn=turn,
        )

    results = await asyncio.gather(prepare(), prepare())
    assert len({result.run_id for result in results}) == 1
    async with db_factory() as session:
        runs = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == base["workspace_id"],
                    AgentRun.thread_id == thread.id,
                )
            )
        )
        turn = await session.scalar(
            select(ThreadTurn).where(
                ThreadTurn.workspace_id == base["workspace_id"],
                ThreadTurn.thread_id == thread.id,
                ThreadTurn.client_token == token,
            )
        )
    assert len(runs) == 1 and turn is not None
    assert turn.agent_run_id == runs[0].id
    assert runs[0].thread_turn_id == turn.id


@pytest.mark.asyncio
async def test_prepared_but_undispatched_turn_is_dispatched_on_retry(db_factory) -> None:
    base, thread = await _thread(db_factory)
    service = ThreadService(db_factory, run_service=AgentRunService(db_factory))
    turn = await service.open_turn(
        base["context"],
        thread.id,
        user_input="prepare after a pre-run crash",
        client_token=uuid4().hex,
    )

    prepared = await service.ensure_stream_run(
        base["context"],
        thread_id=thread.id,
        agent_version_id=base["version"].id,
        turn=turn,
    )
    replay = await service.ensure_stream_run(
        base["context"],
        thread_id=thread.id,
        agent_version_id=base["version"].id,
        turn=turn,
    )

    assert prepared.run_id == replay.run_id
    assert prepared.run is not None
    assert replay.run is not None
    assert replay.run.id == prepared.run.id
    async with db_factory() as session:
        run = await session.get(AgentRun, prepared.run_id)
        stored_turn = await session.get(ThreadTurn, turn.id)
    assert run is not None and stored_turn is not None
    assert run.thread_turn_id == stored_turn.id
    assert stored_turn.agent_run_id == run.id


@pytest.mark.asyncio
async def test_history_is_sequence_bounded_even_when_later_run_finishes_first(
    db_factory,
) -> None:
    from packages.threads.context import SqlAlchemyThreadContextProvider

    base, thread = await _thread(db_factory)
    service = ThreadService(db_factory)
    current_turn = await service.open_turn(
        base["context"], thread.id, user_input="current budget", client_token=uuid4().hex
    )
    future_turn = await service.open_turn(
        base["context"], thread.id, user_input="future release", client_token=uuid4().hex
    )
    # Create the durable runs explicitly so completion order can be reversed.
    async with db_factory() as session:
        current = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            thread_id=thread.id,
            thread_turn_id=current_turn.id,
            input_text=current_turn.user_input,
            final_output="current answer",
            created_by=base["user"].id,
            status="SUCCEEDED",
        )
        future = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            thread_id=thread.id,
            thread_turn_id=future_turn.id,
            input_text=future_turn.user_input,
            final_output="future answer",
            created_by=base["user"].id,
            status="SUCCEEDED",
        )
        session.add_all([current, future])
        current_turn.agent_run_id = current.id
        future_turn.agent_run_id = future.id
        await session.commit()
    provider = SqlAlchemyThreadContextProvider(db_factory)
    conversation = await provider.conversation(
        workspace_id=base["workspace_id"],
        thread_id=thread.id,
        before_run_id=current.id,
        max_turns=10,
    )
    search = await provider.search(
        workspace_id=base["workspace_id"],
        thread_id=thread.id,
        before_run_id=current.id,
        query="future release",
        limit=10,
    )
    assert conversation.turns == ()
    assert search == ()
