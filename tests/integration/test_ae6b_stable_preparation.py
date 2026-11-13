from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunExecutionOverrides, AgentRunService
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.service import ThreadService
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_wiki_native_tool import _publish_wiki, _seed_agent_with_wiki
from tests.support.wiki import seed_wiki

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.integration


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest_asyncio.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    if not TEST_DATABASE_URL:
        pytest.fail("AGENTHUB_TEST_DATABASE_URL is required for S2 stable-preparation tests.")
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


def _evaluation_context(base: dict[str, object]):
    context = base["context"]
    return context.model_copy(update={"permissions": context.permissions | {"evaluation_run"}})


def _with_user(context, user_id: UUID):
    principal = context.organization.principal.model_copy(update={"user_id": str(user_id)})
    organization = context.organization.model_copy(update={"principal": principal})
    return context.model_copy(update={"organization": organization})


@pytest.mark.asyncio
async def test_explicit_thread_identity_reuses_only_an_exact_creation_identity(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-thread-{uuid4().hex}")
        other = await _seed(session, label=f"s2-thread-other-{uuid4().hex}")

    service = ThreadService(db_factory)
    thread_id = uuid4()
    first = await service.create_thread(
        base["context"],
        agent_id=base["version"].agent_id,
        title=" stable thread ",
        kind="support",
        thread_id=thread_id,
    )
    replayed = await service.create_thread(
        base["context"],
        agent_id=base["version"].agent_id,
        title="stable thread",
        kind="support",
        thread_id=thread_id,
    )

    assert first.id == replayed.id == thread_id
    async with db_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentThread)
                .where(
                    AgentThread.workspace_id == base["workspace_id"],
                    AgentThread.id == thread_id,
                )
            )
            == 1
        )

    mismatches = (
        {
            "context": base["context"],
            "agent_id": other["version"].agent_id,
            "title": "stable thread",
            "kind": "support",
        },
        {
            "context": _with_user(base["context"], other["user"].id),
            "agent_id": base["version"].agent_id,
            "title": "stable thread",
            "kind": "support",
        },
        {
            "context": base["context"],
            "agent_id": base["version"].agent_id,
            "title": "stable thread",
            "kind": "general",
        },
        {
            "context": base["context"],
            "agent_id": base["version"].agent_id,
            "title": "different title",
            "kind": "support",
        },
        {
            "context": other["context"],
            "agent_id": other["version"].agent_id,
            "title": "stable thread",
            "kind": "support",
        },
    )
    for mismatch in mismatches:
        with pytest.raises(AgentHubError) as raised:
            await service.create_thread(
                mismatch["context"],
                agent_id=mismatch["agent_id"],
                title=mismatch["title"],
                kind=mismatch["kind"],
                thread_id=thread_id,
            )
        assert raised.value.code == "THREAD_IDEMPOTENCY_CONFLICT"
        assert raised.value.status_code == 409


def _racing_thread_service(db_factory) -> ThreadService:
    seen = 0
    lock = asyncio.Lock()
    both_prechecked = asyncio.Event()

    class _RacingThreadService(ThreadService):
        async def _thread_by_id(self, session, workspace_id, thread_id):
            nonlocal seen
            existing = await super()._thread_by_id(session, workspace_id, thread_id)
            if existing is None:
                async with lock:
                    seen += 1
                    if seen == 2:
                        both_prechecked.set()
                await asyncio.wait_for(both_prechecked.wait(), timeout=5)
            return existing

    return _RacingThreadService(db_factory)


@pytest.mark.asyncio
async def test_explicit_thread_creation_resolves_same_identity_and_cross_identity_races(
    db_factory,
):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-thread-race-{uuid4().hex}")

    thread_id = uuid4()
    same_identity_service = _racing_thread_service(db_factory)
    first, second = await asyncio.gather(
        same_identity_service.create_thread(
            base["context"],
            agent_id=base["version"].agent_id,
            title="concurrent stable",
            kind="support",
            thread_id=thread_id,
        ),
        same_identity_service.create_thread(
            base["context"],
            agent_id=base["version"].agent_id,
            title="concurrent stable",
            kind="support",
            thread_id=thread_id,
        ),
    )
    assert first.id == second.id == thread_id

    conflicting_id = uuid4()
    competing_service = _racing_thread_service(db_factory)
    outcomes = await asyncio.gather(
        competing_service.create_thread(
            base["context"],
            agent_id=base["version"].agent_id,
            title="winner one",
            kind="support",
            thread_id=conflicting_id,
        ),
        competing_service.create_thread(
            base["context"],
            agent_id=base["version"].agent_id,
            title="winner two",
            kind="support",
            thread_id=conflicting_id,
        ),
        return_exceptions=True,
    )
    winners = [row for row in outcomes if isinstance(row, AgentThread)]
    conflicts = [row for row in outcomes if isinstance(row, AgentHubError)]
    assert len(winners) == len(conflicts) == 1
    assert conflicts[0].code == "THREAD_IDEMPOTENCY_CONFLICT"
    assert conflicts[0].status_code == 409

    async with db_factory() as session:
        counts = []
        for expected_id in (thread_id, conflicting_id):
            counts.append(
                await session.scalar(
                    select(func.count())
                    .select_from(AgentThread)
                    .where(
                        AgentThread.workspace_id == base["workspace_id"],
                        AgentThread.id == expected_id,
                    )
                )
            )
    assert counts == [1, 1]


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_raised_unchanged(db_factory, monkeypatch):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-thread-error-{uuid4().hex}")

    class _UnrelatedOrigin:
        diag = SimpleNamespace(constraint_name="agent_threads_pkey_other")

        def __str__(self):
            return 'duplicate key violates unique constraint "agent_threads_pkey_other"'

    failure = IntegrityError("INSERT INTO agent_threads", {}, _UnrelatedOrigin())

    async def fail_commit(_session):
        raise failure

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(IntegrityError) as raised:
        await ThreadService(db_factory).create_thread(
            base["context"],
            agent_id=base["version"].agent_id,
            title="unrelated failure",
            kind="support",
            thread_id=uuid4(),
        )
    assert raised.value is failure


@pytest.mark.asyncio
async def test_prepare_first_recovers_an_unbound_turn_run_from_reserved_identity(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-recover-{uuid4().hex}")

    context = _evaluation_context(base)
    case_id = uuid4()
    instance_id = case_id.hex
    thread_id = uuid4()
    first_input = "opening question"
    runtime = AgentRunService(db_factory)
    threads = ThreadService(db_factory, run_service=runtime)
    thread = await threads.create_thread(
        context,
        agent_id=base["version"].agent_id,
        title=f"evaluation-{instance_id}",
        kind="support",
        thread_id=thread_id,
    )
    turn = await threads.open_turn(
        context,
        thread.id,
        user_input=first_input,
        client_token=f"{instance_id}:0",
    )
    run = await runtime.prepare_run(
        context,
        agent_version_id=base["version"].id,
        input_text=first_input,
        execution_overrides=AgentRunExecutionOverrides.for_evaluation([]),
        thread_id=thread.id,
        thread_turn_id=turn.id,
    )
    assert turn.agent_run_id is None

    prepared = await ScriptedScenarioDriver(runtime, driver_kind="controlled").prepare_first(
        context,
        agent_version_id=base["version"].id,
        first_input=first_input,
        knowledge_snapshots=[],
        instance_id=instance_id,
        thread_id=thread_id,
    )

    assert prepared.instance_id == instance_id
    assert prepared.thread_id == thread_id
    assert prepared.turn_id == turn.id
    assert prepared.run_id == run.id
    async with db_factory() as session:
        recovered = await session.get(ThreadTurn, turn.id)
        assert recovered is not None and recovered.agent_run_id == run.id


@pytest.mark.asyncio
async def test_prepare_first_is_concurrently_idempotent_and_rejects_changed_input(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-prepare-{uuid4().hex}")

    context = _evaluation_context(base)
    case_id = uuid4()
    instance_id = case_id.hex
    thread_id = uuid4()
    driver = ScriptedScenarioDriver(AgentRunService(db_factory), driver_kind="controlled")

    async def prepare():
        return await driver.prepare_first(
            context,
            agent_version_id=base["version"].id,
            first_input="stable first turn",
            knowledge_snapshots=[],
            instance_id=instance_id,
            thread_id=thread_id,
        )

    first, second = await asyncio.gather(prepare(), prepare())
    assert (first.thread_id, first.turn_id, first.run_id) == (
        second.thread_id,
        second.turn_id,
        second.run_id,
    )

    with pytest.raises(AgentHubError) as changed:
        await driver.prepare_first(
            context,
            agent_version_id=base["version"].id,
            first_input="changed first turn",
            knowledge_snapshots=[],
            instance_id=instance_id,
            thread_id=thread_id,
        )
    assert changed.value.code == "THREAD_TURN_IDEMPOTENCY_CONFLICT"
    assert changed.value.status_code == 409

    async with db_factory() as session:
        turn_rows = list(
            await session.scalars(
                select(ThreadTurn)
                .where(ThreadTurn.thread_id == thread_id)
                .order_by(ThreadTurn.sequence)
            )
        )
        run_count = await session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.thread_id == thread_id)
        )
    assert len(turn_rows) == 1
    assert turn_rows[0].client_token == f"{case_id.hex}:0"
    assert turn_rows[0].agent_run_id == first.run_id
    assert run_count == 1


@pytest.mark.asyncio
async def test_prepare_first_requires_complete_stable_identity(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-partial-{uuid4().hex}")

    driver = ScriptedScenarioDriver(AgentRunService(db_factory), driver_kind="controlled")
    invalid = (
        ({"instance_id": uuid4().hex}, "SCENARIO_STABLE_IDENTITY_INCOMPLETE"),
        ({"thread_id": uuid4()}, "SCENARIO_STABLE_IDENTITY_INCOMPLETE"),
        (
            {"instance_id": "", "thread_id": uuid4()},
            "SCENARIO_INSTANCE_ID_INVALID",
        ),
    )
    for kwargs, expected in invalid:
        with pytest.raises(ValueError, match=expected):
            await driver.prepare_first(
                _evaluation_context(base),
                agent_version_id=base["version"].id,
                first_input="identity validation",
                knowledge_snapshots=[],
                **kwargs,
            )


@pytest.mark.asyncio
async def test_prepare_first_legacy_call_keeps_generated_identity_convention(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-legacy-{uuid4().hex}")

    driver = ScriptedScenarioDriver(AgentRunService(db_factory), driver_kind="controlled")
    prepared = await driver.prepare_first(
        _evaluation_context(base),
        agent_version_id=base["version"].id,
        first_input="legacy opening",
        knowledge_snapshots=[],
    )

    assert len(prepared.instance_id) == 32
    UUID(prepared.instance_id)
    async with db_factory() as session:
        turn = await session.get(ThreadTurn, prepared.turn_id)
    assert turn is not None
    assert turn.client_token == f"{prepared.instance_id}:0"


class _CapturingFrozenRuntime(AgentRunService):
    def __init__(self, session_factory):
        super().__init__(session_factory)
        self.prepare_calls: list[dict[str, object]] = []

    async def prepare_run(
        self,
        context,
        *,
        agent_version_id,
        input_text,
        execution_overrides=None,
        thread_id=None,
        thread_turn_id=None,
    ):
        assert execution_overrides is not None
        knowledge = [dict(item) for item in execution_overrides.effective_knowledge_snapshots]
        wiki = [dict(item) for item in execution_overrides.effective_wiki_publications]
        self.prepare_calls.append({"input_text": input_text, "knowledge": knowledge, "wiki": wiki})
        run = await super().prepare_run(
            context,
            agent_version_id=agent_version_id,
            input_text=input_text,
            execution_overrides=AgentRunExecutionOverrides.for_evaluation([]),
            thread_id=thread_id,
            thread_turn_id=thread_turn_id,
        )
        # The fixture version intentionally has no live Wiki/KB resources. Persist
        # the supplied frozen envelope here to test the driver/runtime seam.
        async with self.session_factory() as session:
            stored = await session.get(AgentRun, run.id)
            assert stored is not None
            stored.effective_knowledge_snapshots = knowledge
            stored.effective_wiki_publications = wiki
            await session.commit()
        return run

    async def execute_prepared_run(self, context, *, run_id):
        async with self.session_factory() as session:
            stored = await session.get(AgentRun, run_id)
            assert stored is not None
            stored.status = "SUCCEEDED"
            stored.final_output = "fixture final"
            await session.commit()
        return SimpleNamespace(run_id=run_id, status="SUCCEEDED")


@pytest.mark.asyncio
async def test_case_identity_tokens_and_all_frozen_bindings_flow_to_every_turn(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"s2-frozen-{uuid4().hex}")

    context = _evaluation_context(base)
    case_id = uuid4()
    instance_id = case_id.hex
    thread_id = uuid4()
    knowledge = [
        {
            "knowledge_base_id": str(uuid4()),
            "snapshot_id": str(uuid4()),
            "snapshot_hash": "a" * 64,
            "binding_mode": "PINNED",
        }
    ]
    wiki = [
        {
            "knowledge_base_id": str(uuid4()),
            "binding_mode": "PINNED",
            "wiki_publication_id": str(uuid4()),
            "publication_hash": "b" * 64,
        }
    ]
    runtime = _CapturingFrozenRuntime(db_factory)
    driver = ScriptedScenarioDriver(runtime, driver_kind="controlled")
    prepared = await driver.prepare_first(
        context,
        agent_version_id=base["version"].id,
        first_input="opening",
        knowledge_snapshots=knowledge,
        wiki_publications=wiki,
        instance_id=instance_id,
        thread_id=thread_id,
    )
    observation = await driver.execute(
        context,
        case_id=str(case_id),
        agent_version_id=base["version"].id,
        user_turns=["opening", "follow-up"],
        knowledge_snapshots=knowledge,
        wiki_publications=wiki,
        prepared_first=prepared,
    )

    assert observation["instance_id"] == instance_id
    assert observation["thread_id"] == str(thread_id)
    assert observation["executed_turn_count"] == 2
    assert runtime.prepare_calls == [
        {"input_text": "opening", "knowledge": knowledge, "wiki": wiki},
        {"input_text": "follow-up", "knowledge": knowledge, "wiki": wiki},
    ]
    async with db_factory() as session:
        turns = list(
            await session.scalars(
                select(ThreadTurn)
                .where(ThreadTurn.thread_id == thread_id)
                .order_by(ThreadTurn.sequence)
            )
        )
        runs = list(
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.thread_id == thread_id)
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
    assert [turn.client_token for turn in turns] == [
        f"{case_id.hex}:0",
        f"{case_id.hex}:1",
    ]
    assert len(runs) == 2
    assert all(run.effective_knowledge_snapshots == knowledge for run in runs)
    assert all(run.effective_wiki_publications == wiki for run in runs)


@pytest.mark.asyncio
async def test_prepare_first_persists_a_live_frozen_wiki_publication(db_factory):
    context, knowledge_base_id, profile_id, _ = await seed_wiki(db_factory)
    publication = await _publish_wiki(
        db_factory,
        context,
        knowledge_base_id,
        profile_id,
    )
    _, version = await _seed_agent_with_wiki(
        db_factory,
        context,
        knowledge_base_id,
        profile_id,
        publication,
    )
    assert version is not None
    frozen_context = context.model_copy(
        update={"permissions": context.permissions | frozenset({"evaluation_run", "knowledge_run"})}
    )
    case_id = uuid4()
    instance_id = case_id.hex
    thread_id = uuid4()
    wiki = [
        {
            "knowledge_base_id": str(knowledge_base_id),
            "binding_mode": "PINNED",
            "wiki_publication_id": str(publication.id),
            "publication_hash": publication.content_hash,
        }
    ]

    prepared = await ScriptedScenarioDriver(
        AgentRunService(db_factory), driver_kind="controlled"
    ).prepare_first(
        frozen_context,
        agent_version_id=version.id,
        first_input="opening with frozen wiki",
        knowledge_snapshots=[],
        wiki_publications=wiki,
        instance_id=instance_id,
        thread_id=thread_id,
    )

    async with db_factory() as session:
        run = await session.get(AgentRun, prepared.run_id)
    assert run is not None
    assert run.effective_wiki_publications == wiki
