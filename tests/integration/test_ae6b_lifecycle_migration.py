"""PostgreSQL migration protections for durable interactive execution evidence."""

from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.core.database import create_database
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.models import InteractiveEpisode, InteractiveEpisodeTurn
from tests.support.ae6b_database_urls import (
    async_test_database_url,
    sync_test_database_url,
)
from tests.support.ae6b_lifecycle import (
    create_bound_episode_turn,
    create_owned_lifecycle_cases,
    reserve_episode,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Connect to the already-migrated isolated PostgreSQL database."""

    database_url = async_test_database_url(TEST_DATABASE_URL)
    engine, factory = create_database(database_url.render_as_string(hide_password=False))
    try:
        yield factory
    finally:
        await engine.dispose()


def _completed_observation(agent_run_id: UUID) -> dict[str, Any]:
    return {
        "observation": {
            "category": "INTERACTIVE",
            "execution_complete": True,
            "turns": [{"status": "SUCCEEDED"}],
        },
        "agent_run_id": str(agent_run_id),
        "latency_ms": 10,
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
        "cached_tokens": 0,
        "cost_amount": None,
        "cost_currency": None,
        "aggregate_billing_complete": False,
        "failure_code": None,
        "safe_failure_message": None,
    }


def _assert_trigger_rejection(error: DBAPIError, message: str) -> None:
    sqlstate = getattr(error.orig, "sqlstate", None) or getattr(
        error.orig,
        "pgcode",
        None,
    )
    assert sqlstate == "P0001"
    assert message in str(error.orig)


async def _expect_direct_write_rejected(
    session: AsyncSession,
    *,
    statement: str,
    parameters: Mapping[str, Any],
    message: str,
) -> None:
    with pytest.raises(DBAPIError) as rejected:
        async with session.begin_nested():
            await session.execute(text(statement), dict(parameters))
    _assert_trigger_rejection(rejected.value, message)


@pytest.mark.asyncio
async def test_0051_guards_retained_episode_and_rejects_nested_downgrade(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = (
        await create_owned_lifecycle_cases(
            db_factory,
            workspace_count=1,
            repetitions=1,
        )
    )[0]
    lifecycle = InteractiveLifecycle(db_factory)
    episode = await reserve_episode(lifecycle, owned)
    episode = await lifecycle.create_fixture(owned.identity)
    bound = await create_bound_episode_turn(
        db_factory,
        owned,
        episode,
        opening="Migration guard evidence.",
    )
    await lifecycle.dispatch(owned.identity, turn_index=0)
    async with db_factory() as session:
        agent_run = await session.get(AgentRun, bound.run.id)
        assert agent_run is not None
        agent_run.status = "SUCCEEDED"
        await session.commit()

    evidence = {
        "run_id": str(bound.run.id),
        "thread_turn_id": str(bound.turn.id),
        "observed_agent_status": "SUCCEEDED",
        "failure_code": None,
    }
    await lifecycle.record_turn(owned.identity, turn_index=0, evidence=evidence)
    assertion = {
        "passed": True,
        "observed": {"ticket_count": 0, "ticket_statuses": []},
        "reason": None,
    }
    observation = _completed_observation(bound.run.id)
    await lifecycle.retain_assertion(
        owned.identity,
        assertion=assertion,
        source="migration_guard.assert_state",
    )
    await lifecycle.retain_observation(owned.identity, envelope=observation)

    async with db_factory() as session:
        stored_episode = await session.get(InteractiveEpisode, owned.identity.case_id)
        stored_turn = await session.scalar(
            select(InteractiveEpisodeTurn).where(
                InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                InteractiveEpisodeTurn.turn_index == 0,
            )
        )
        assert stored_episode is not None
        assert stored_turn is not None
        assert stored_episode.thread_id == bound.thread.id
        assert stored_episode.assertion_hash is not None
        assert stored_episode.observation_hash is not None
        assert stored_turn.agent_run_id == bound.run.id
        assert stored_turn.evidence_hash is not None
        episode_before = {
            "case_id": stored_episode.case_id,
            "workspace_id": stored_episode.workspace_id,
            "instance_id": stored_episode.instance_id,
            "reserved_thread_id": stored_episode.reserved_thread_id,
            "thread_id": stored_episode.thread_id,
            "seed": stored_episode.seed,
            "assertion": stored_episode.assertion,
            "assertion_hash": stored_episode.assertion_hash,
            "observation_envelope": stored_episode.observation_envelope,
            "observation_hash": stored_episode.observation_hash,
        }
        turn_before = {
            "id": stored_turn.id,
            "workspace_id": stored_turn.workspace_id,
            "case_id": stored_turn.case_id,
            "thread_turn_id": stored_turn.thread_turn_id,
            "agent_run_id": stored_turn.agent_run_id,
            "evidence": stored_turn.evidence,
            "evidence_hash": stored_turn.evidence_hash,
        }

    replacement_thread_id = uuid4()
    replacement_run_id = uuid4()
    parameters = {
        "case_id": owned.identity.case_id,
        "workspace_id": owned.identity.workspace_id,
        "turn_id": turn_before["id"],
        "replacement_thread_id": replacement_thread_id,
        "replacement_run_id": replacement_run_id,
        "replacement_hash": "f" * 64,
    }
    async with db_factory() as session:
        await _expect_direct_write_rejected(
            session,
            statement=(
                "UPDATE evaluation_interactive_episodes "
                "SET seed = seed + 1 WHERE case_id = :case_id"
            ),
            parameters=parameters,
            message="Interactive episode identity is immutable",
        )
        await _expect_direct_write_rejected(
            session,
            statement=(
                "UPDATE evaluation_interactive_episodes "
                "SET thread_id = :replacement_thread_id WHERE case_id = :case_id"
            ),
            parameters=parameters,
            message="Interactive episode thread binding is immutable",
        )
        await _expect_direct_write_rejected(
            session,
            statement=(
                "UPDATE evaluation_interactive_episodes "
                "SET assertion_source = 'tampered' WHERE case_id = :case_id"
            ),
            parameters=parameters,
            message="Interactive assertion evidence is immutable",
        )
        await _expect_direct_write_rejected(
            session,
            statement=(
                "UPDATE evaluation_interactive_episodes "
                "SET observation_hash = :replacement_hash WHERE case_id = :case_id"
            ),
            parameters=parameters,
            message="Interactive observation evidence is immutable",
        )
        await _expect_direct_write_rejected(
            session,
            statement=(
                "UPDATE evaluation_interactive_episode_turns "
                "SET agent_run_id = :replacement_run_id WHERE id = :turn_id"
            ),
            parameters=parameters,
            message="Interactive turn AgentRun binding is immutable",
        )
        await _expect_direct_write_rejected(
            session,
            statement=(
                "UPDATE evaluation_interactive_episode_turns "
                "SET evidence = '{\"tampered\": true}'::jsonb, "
                "evidence_hash = :replacement_hash WHERE id = :turn_id"
            ),
            parameters=parameters,
            message="Interactive turn evidence is immutable",
        )
        await _expect_direct_write_rejected(
            session,
            statement="DELETE FROM evaluation_interactive_episode_turns WHERE id = :turn_id",
            parameters=parameters,
            message="Cannot remove retained interactive execution evidence",
        )
        await _expect_direct_write_rejected(
            session,
            statement="DELETE FROM evaluation_interactive_episodes WHERE case_id = :case_id",
            parameters=parameters,
            message="Cannot remove retained interactive execution evidence",
        )
        await session.rollback()

    async with db_factory() as session:
        episode_after = await session.get(InteractiveEpisode, owned.identity.case_id)
        turn_after = await session.scalar(
            select(InteractiveEpisodeTurn).where(
                InteractiveEpisodeTurn.workspace_id == owned.identity.workspace_id,
                InteractiveEpisodeTurn.case_id == owned.identity.case_id,
                InteractiveEpisodeTurn.turn_index == 0,
            )
        )
        assert episode_after is not None
        assert turn_after is not None
        assert {
            "case_id": episode_after.case_id,
            "workspace_id": episode_after.workspace_id,
            "instance_id": episode_after.instance_id,
            "reserved_thread_id": episode_after.reserved_thread_id,
            "thread_id": episode_after.thread_id,
            "seed": episode_after.seed,
            "assertion": episode_after.assertion,
            "assertion_hash": episode_after.assertion_hash,
            "observation_envelope": episode_after.observation_envelope,
            "observation_hash": episode_after.observation_hash,
        } == episode_before
        assert {
            "id": turn_after.id,
            "workspace_id": turn_after.workspace_id,
            "case_id": turn_after.case_id,
            "thread_turn_id": turn_after.thread_turn_id,
            "agent_run_id": turn_after.agent_run_id,
            "evidence": turn_after.evidence,
            "evidence_hash": turn_after.evidence_hash,
        } == turn_before

    migration = importlib.import_module("migrations.versions.0051_interactive_lifecycle")
    sync_engine = create_engine(sync_test_database_url(TEST_DATABASE_URL))
    try:
        with sync_engine.connect() as connection:
            version_before = connection.scalar(text("SELECT version_num FROM alembic_version"))
            scripts = ScriptDirectory.from_config(Config("alembic.ini"))
            assert version_before == scripts.get_current_head()
            assert any(
                revision.revision == "0051_interactive_lifecycle"
                for revision in scripts.iterate_revisions(version_before, "base")
            )
            monkeypatch.setattr(
                migration,
                "op",
                Operations(MigrationContext.configure(connection)),
            )
            with pytest.raises(DBAPIError) as rejected:
                with connection.begin_nested():
                    migration.downgrade()
            _assert_trigger_rejection(
                rejected.value,
                "Cannot remove retained interactive execution evidence",
            )

            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == version_before
            )
            retained_episode = (
                connection.execute(
                    text(
                        """
                        SELECT case_id, workspace_id, instance_id,
                               reserved_thread_id, thread_id, seed,
                               assertion, assertion_hash,
                               observation_envelope, observation_hash
                        FROM evaluation_interactive_episodes
                        WHERE case_id = :case_id
                        """
                    ),
                    {"case_id": owned.identity.case_id},
                )
                .mappings()
                .one()
            )
            retained_turn = (
                connection.execute(
                    text(
                        """
                        SELECT id, workspace_id, case_id, thread_turn_id,
                               agent_run_id, evidence, evidence_hash
                        FROM evaluation_interactive_episode_turns
                        WHERE id = :turn_id
                        """
                    ),
                    {"turn_id": turn_before["id"]},
                )
                .mappings()
                .one()
            )
            assert retained_episode["case_id"] == episode_before["case_id"]
            assert retained_episode["workspace_id"] == episode_before["workspace_id"]
            assert retained_episode["instance_id"] == episode_before["instance_id"]
            assert retained_episode["reserved_thread_id"] == episode_before["reserved_thread_id"]
            assert retained_episode["thread_id"] == episode_before["thread_id"]
            assert retained_episode["seed"] == episode_before["seed"]
            assert retained_episode["assertion"] == episode_before["assertion"]
            assert retained_episode["assertion_hash"] == episode_before["assertion_hash"]
            assert (
                retained_episode["observation_envelope"] == episode_before["observation_envelope"]
            )
            assert retained_episode["observation_hash"] == episode_before["observation_hash"]
            assert retained_turn["id"] == turn_before["id"]
            assert retained_turn["workspace_id"] == turn_before["workspace_id"]
            assert retained_turn["case_id"] == turn_before["case_id"]
            assert retained_turn["thread_turn_id"] == turn_before["thread_turn_id"]
            assert retained_turn["agent_run_id"] == turn_before["agent_run_id"]
            assert retained_turn["evidence"] == turn_before["evidence"]
            assert retained_turn["evidence_hash"] == turn_before["evidence_hash"]
    finally:
        sync_engine.dispose()
