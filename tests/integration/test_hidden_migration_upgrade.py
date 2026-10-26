"""Exercise new migrations against historical Turn data, in a dedicated DB."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.threads.models import AgentThread, ThreadTurn
from packages.threads.service import ThreadService
from tests.integration.test_m4c_agent_runtime import _seed

DATABASE_URL = os.environ.get("AGENTHUB_MIGRATION_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="Dedicated migration DB required"),
]


def _drop_test_schema() -> None:
    """Reset only an explicitly disposable migration database.

    Historical migration 0011 intentionally refuses to reverse its data
    backfill, so walking to ``base`` cannot reset a reused test database. A
    schema recreation is isolated from the production migration graph and lets
    every run seed a real 0034 schema from scratch.
    """

    async def reset():
        engine, _ = create_database(DATABASE_URL)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("DROP SCHEMA public CASCADE"))
                await connection.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    asyncio.run(reset())


def test_populated_upgrade_downgrade_and_request_replay(monkeypatch):
    # Downgrades run only on an explicitly named disposable migration DB.
    assert DATABASE_URL.rsplit("/", 1)[-1].startswith("agenthub_hidden_defects_")
    assert DATABASE_URL.endswith("_migration")
    monkeypatch.setenv("AGENTHUB_DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    # ``upgrade`` is not a downgrade. Reusing this explicitly disposable DB
    # after an earlier run must still rebuild the schema before seeding it.
    # 0011 is deliberately irreversible, so reset this test DB's schema.
    _drop_test_schema()
    command.upgrade(config, "0034_wiki_native_binding")

    async def seed_legacy():
        engine, factory = create_database(DATABASE_URL)
        try:
            async with factory() as session:
                base = await _seed(session, label=uuid4().hex)
                thread = AgentThread(
                    workspace_id=base["workspace_id"],
                    agent_id=base["version"].agent_id,
                    title="历史会话",
                    created_by=base["user"].id,
                )
                session.add(thread)
                await session.flush()
                run_id, turn_id, token = uuid4(), uuid4(), uuid4().hex
                await session.execute(
                    text(
                        "INSERT INTO agent_runs (id, workspace_id, agent_version_id, thread_id, "
                        "input_text, created_by, status) VALUES "
                        "(:id,:workspace,:version,:thread,:input,:actor,'SUCCEEDED')"
                    ),
                    {
                        "id": run_id,
                        "workspace": thread.workspace_id,
                        "version": base["version"].id,
                        "thread": thread.id,
                        "input": "历史问题",
                        "actor": base["user"].id,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO thread_turns (id,workspace_id,thread_id,sequence,user_input,"
                        "agent_run_id,client_token) VALUES "
                        "(:id,:workspace,:thread,1,:input,:run,:token)"
                    ),
                    {
                        "id": turn_id,
                        "workspace": thread.workspace_id,
                        "thread": thread.id,
                        "input": "历史问题",
                        "run": run_id,
                        "token": token,
                    },
                )
                # Null request tokens existed before this change.
                await session.execute(
                    text(
                        "INSERT INTO thread_turns (id,workspace_id,thread_id,sequence,user_input) "
                        "VALUES (:id,:workspace,:thread,2,:input)"
                    ),
                    {
                        "id": uuid4(),
                        "workspace": thread.workspace_id,
                        "thread": thread.id,
                        "input": "无token旧问题",
                    },
                )
                shared_run = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO agent_runs (id,workspace_id,agent_version_id,thread_id,"
                        "input_text,created_by,status) VALUES "
                        "(:id,:workspace,:version,:thread,'共享的旧问题',:actor,'SUCCEEDED')"
                    ),
                    {
                        "id": shared_run,
                        "workspace": thread.workspace_id,
                        "version": base["version"].id,
                        "thread": thread.id,
                        "actor": base["user"].id,
                    },
                )
                for sequence in (3, 4):
                    await session.execute(
                        text(
                            "INSERT INTO thread_turns (id,workspace_id,thread_id,sequence,"
                            "user_input,"
                            "agent_run_id,client_token) VALUES "
                            "(:id,:workspace,:thread,:sequence,'共享的旧问题',:run,:token)"
                        ),
                        {
                            "id": uuid4(),
                            "workspace": thread.workspace_id,
                            "thread": thread.id,
                            "sequence": sequence,
                            "run": shared_run,
                            "token": uuid4().hex,
                        },
                    )
                active_run = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO agent_runs (id,workspace_id,agent_version_id,input_text,"
                        "created_by,status) VALUES (:id,:workspace,:version,'legacy active',"
                        ":actor,'RUNNING')"
                    ),
                    {
                        "id": active_run,
                        "workspace": thread.workspace_id,
                        "version": base["version"].id,
                        "actor": base["user"].id,
                    },
                )
                await session.commit()
                return (
                    base["context"],
                    thread.id,
                    turn_id,
                    run_id,
                    token,
                    base["version"].id,
                    active_run,
                )
        finally:
            await engine.dispose()

    context, thread_id, turn_id, run_id, token, version_id, active_run = asyncio.run(seed_legacy())
    command.upgrade(config, "head")

    async def verify_replay():
        engine, factory = create_database(DATABASE_URL)
        try:
            turn = await ThreadService(factory).open_turn(
                context, thread_id, user_input="历史问题", client_token=token
            )
            assert turn.id == turn_id and turn.agent_run_id == run_id
            async with factory() as session:
                bound = await session.scalar(
                    text("SELECT thread_turn_id FROM agent_runs WHERE id=:id"), {"id": run_id}
                )
                assert bound == turn_id
                fenced = (
                    await session.execute(
                        text(
                            "SELECT status, failure_code, execution_owner "
                            "FROM agent_runs WHERE id=:id"
                        ),
                        {"id": active_run},
                    )
                ).one()
                assert fenced.status == "NEEDS_ATTENTION"
                assert fenced.failure_code == "LEGACY_RUN_OWNERSHIP_UNKNOWN"
                assert fenced.execution_owner is None
                assert (
                    await session.scalar(
                        text("SELECT event_log_failed FROM agent_runs WHERE id=:id"), {"id": run_id}
                    )
                    is True
                )
                assert await session.scalar(
                    text(
                        "SELECT bool_and(input_hash ~ '^[0-9a-f]{64}$') "
                        "FROM thread_turns WHERE thread_id=:id"
                    ),
                    {"id": thread_id},
                )
                new_turn_default = await session.scalar(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name='thread_turns' "
                        "AND column_name='legacy_recovery_required'"
                    )
                )
                assert new_turn_default in {"false", "'false'", "false::boolean"}
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM thread_turns WHERE thread_id=:id"),
                        {"id": thread_id},
                    )
                    == 4
                )
                quarantined = list(
                    await session.scalars(
                        select(ThreadTurn).where(
                            ThreadTurn.thread_id == thread_id, ThreadTurn.id != turn_id
                        )
                    )
                )
                assert len(quarantined) == 3 and all(
                    row.legacy_recovery_required for row in quarantined
                )
            service = ThreadService(factory, run_service=object())
            for old in quarantined:
                with pytest.raises(AgentHubError) as error:
                    await service.ensure_stream_run(
                        context, thread_id=thread_id, agent_version_id=version_id, turn=old
                    )
                assert error.value.code == "THREAD_TURN_RECOVERY_REQUIRED"
        finally:
            await engine.dispose()

    asyncio.run(verify_replay())
    command.downgrade(config, "0034_wiki_native_binding")
    command.upgrade(config, "head")
    asyncio.run(verify_replay())

    async def verify_new_turn_default():
        engine, factory = create_database(DATABASE_URL)
        try:
            workspace_id = UUID(context.workspace_id)
            # Use the seeded thread/workspace without fabricating recovery
            # evidence: the column default must be false independent of the
            # historical backfill flag.
            async with factory() as session:
                inserted = await session.scalar(
                    text(
                        "INSERT INTO thread_turns "
                        "(id,workspace_id,thread_id,sequence,user_input,client_token,input_hash) "
                        "VALUES (:id,:workspace,:thread,5,'new question',:token,:hash) "
                        "RETURNING legacy_recovery_required"
                    ),
                    {
                        "id": uuid4(),
                        "workspace": workspace_id,
                        "thread": thread_id,
                        "token": uuid4().hex,
                        "hash": "0" * 64,
                    },
                )
                await session.rollback()
            assert inserted is False
        finally:
            await engine.dispose()

    asyncio.run(verify_new_turn_default())

    async def verify_completed_thread_deletion():
        engine, factory = create_database(DATABASE_URL)
        try:
            await ThreadService(factory).delete_thread(context, thread_id)
            async with factory() as session:
                retained = (
                    await session.execute(
                        text("SELECT thread_id, thread_turn_id FROM agent_runs WHERE id=:id"),
                        {"id": run_id},
                    )
                ).one()
                assert retained.thread_id is None and retained.thread_turn_id is None
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM thread_turns WHERE thread_id=:id"),
                        {"id": thread_id},
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(verify_completed_thread_deletion())
    get_settings.cache_clear()
