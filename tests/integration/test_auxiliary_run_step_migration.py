"""Opt-in, isolated PostgreSQL acceptance of existing auxiliary step writers.

AGENTHUB_TEST_ADMIN_DATABASE_URL must name local PostgreSQL. This test creates
unique databases and retains them for audit; it never migrates the supplied DB.
Provider calls are replaced by the existing deterministic selector fixture.
"""

import asyncio
import os
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from packages.agent_runtime.models import AgentRun, RunStep
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_runtime.runtime_service import AgentRunService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.tools.contracts import ToolResult
from tests.integration.test_m4c_agent_runtime import _seed
from tests.unit.test_complementary_search_product import fixture
from tests.unit.test_runtime_rag_evidence_projection import _graph

ADMIN = os.environ.get("AGENTHUB_TEST_ADMIN_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ADMIN, reason="Local admin URL required"),
]


def _migrate(url, target, *, downgrade=False):
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    try:
        (command.downgrade if downgrade else command.upgrade)(Config("alembic.ini"), target)
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


def _snapshot(engine):
    with engine.connect() as conn:
        return {
            "version": conn.scalar(text("SELECT version_num FROM alembic_version")),
            "rows": conn.execute(text("SELECT row_to_json(s)::text FROM run_steps s ORDER BY id"))
            .scalars()
            .all(),
            "indexes": conn.execute(
                text(
                    "SELECT indexrelid, pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indrelid = 'run_steps'::regclass ORDER BY indexrelid"
                )
            ).all(),
            "constraint": conn.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'run_steps'::regclass AND conname = 'ck_run_steps_kind'"
                )
            ),
        }


async def _seed_run(factory):
    async with factory() as session:
        base = await _seed(session, label="auxiliary-step-migration")
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            status="RUNNING",
            input_text="Synthetic migration acceptance",
            created_by=base["user"].id,
        )
        session.add(run)
        await session.flush()
        ids = run.id, run.workspace_id, run.agent_version_id
        session.add(
            RunStep(
                workspace_id=run.workspace_id,
                agent_run_id=run.id,
                sequence_number=0,
                kind="PREPARE",
                status="SUCCEEDED",
                safe_metadata={"synthetic": True},
            )
        )
        await session.commit()
        return ids


async def _exercise(factory, mode):
    from benchmarks.public_rag.consumer_replay import _Session
    from packages.tools.builtins.search_knowledge import search_knowledge

    ids = await _seed_run(factory)
    ctx, tool, base, gateway = fixture(
        output='{"selected_ids":[42]}' if mode == "failure" else '{"selected_ids":[2]}',
        opt_in=mode != "disabled",
    )
    graph = _graph()
    graph.context = SimpleNamespace(workspace_id=str(ids[1]))
    graph.run = SimpleNamespace(id=ids[0], agent_version_id=ids[2], thread_id=None)
    graph.service.session_factory = factory
    graph.service._append_step = partial(AgentRunService._append_step, graph.service)
    graph.step = partial(_AgentRunGraph.step, graph)
    graph.service.model_gateway_factory = lambda _: SimpleNamespace(
        prepare_resolved=AsyncMock(return_value=gateway)
    )
    graph.service.artifact_recorder = None

    async def execute(**kwargs):
        data = await search_knowledge(
            replace(ctx, selector_binding=kwargs.get("selector_binding")),
            tool,
            kwargs["arguments"],
            _Session,
            retriever=base,
        )
        return ToolResult.success(data)

    graph.service.tool_runtime = SimpleNamespace(execute=execute)
    graph._stop_if_requested = AsyncMock(return_value=None)
    graph._emit = AsyncMock()
    runtime = {"max_parallel_reads": 1}
    if mode == "budget":
        runtime["max_cost_micro_usd"] = 1
    update = await graph.read_execute(
        {
            "spec": SimpleNamespace(model_plan=ctx.selector_binding[1]),
            "tool_definitions": {"search_knowledge": tool},
            "pending_tool_calls": [
                {
                    "name": "search_knowledge",
                    "arguments": {"query": "q", "limit": 2},
                    "tool_call_id": "synthetic-call",
                }
            ],
            "runtime": runtime,
            "model_round_count": 1,
            "usage_records": [],
        }
    )
    async with factory() as session:
        steps = list(
            await session.scalars(
                select(RunStep)
                .where(RunStep.agent_run_id == ids[0])
                .order_by(RunStep.sequence_number)
            )
        )
    kinds = [step.kind for step in steps]
    data = update["executed_observations"]["synthetic-call"].data
    if mode == "disabled":
        assert kinds == ["PREPARE", "TOOL_EXECUTE"]
        assert "evidence_selection" not in data
        gateway.generate_resolved.assert_not_awaited()
    else:
        assert "EVIDENCE_SELECTION" in kinds
        receipt = data["evidence_selection"][0]
        if mode == "budget":
            assert "AUXILIARY_MODEL" not in kinds
            assert receipt["reason"] == "cost_budget_unpriced_selector"
            gateway.generate_resolved.assert_not_awaited()
        else:
            assert "AUXILIARY_MODEL" in kinds
            gateway.generate_resolved.assert_awaited_once()
            assert receipt["status"] == ("fallback" if mode == "failure" else "selected")
            assert update["usage_records"][0]["usage"]["total_tokens"] == 38
    return [record["chunk_id"] for record in data["results"]]


async def _seed_history(factory):
    run_id, workspace_id, _ = await _seed_run(factory)
    historical = (
        "MODEL",
        "TOOL_PROPOSAL",
        "POLICY",
        "TOOL_EXECUTE",
        "OBSERVATION",
        "APPROVAL_WAIT",
        "ACTION",
        "RECONCILIATION",
        "SUMMARY",
        "GUARD",
        "FINISH",
    )
    async with factory() as session:
        for sequence, kind in enumerate(historical, start=1):
            session.add(
                RunStep(
                    workspace_id=workspace_id,
                    agent_run_id=run_id,
                    sequence_number=sequence,
                    kind=kind,
                    status="SUCCEEDED",
                    safe_metadata={"synthetic": True},
                )
            )
        await session.commit()


@pytest.mark.parametrize("path", ["upgrade", "fresh"])
def test_isolated_postgresql_full_migrations_runtime_and_safe_downgrade(path):
    admin = make_url(ADMIN)
    assert admin.host in {"localhost", "127.0.0.1", "::1"}
    admin = admin.set(host="127.0.0.1")
    name = "agenthub_step_accept_" + path + "_" + uuid4().hex[:12]
    admin_engine = create_engine(
        admin.set(drivername="postgresql+psycopg", database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin_engine.dispose()
    url = admin.set(database=name, drivername="postgresql+psycopg")
    engine = create_engine(url)

    async def with_factory(action):
        db_engine, factory = create_database(
            url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        )
        try:
            return await action(factory)
        finally:
            await db_engine.dispose()

    try:
        _migrate(url, "0055_support_records" if path == "upgrade" else "head")
        asyncio.run(with_factory(_seed_history))
        before = _snapshot(engine)
        if path == "upgrade":
            _migrate(url, "head")
        after = _snapshot(engine)
        assert after["version"] == "0056_auxiliary_run_steps"
        assert after["rows"] == before["rows"] and after["indexes"] == before["indexes"]
        # With no new-kind rows, downgrade and re-upgrade preserve all evidence.
        _migrate(url, "0055_support_records", downgrade=True)
        reverted = _snapshot(engine)
        assert reverted["rows"] == before["rows"] and reverted["indexes"] == before["indexes"]
        assert "AUXILIARY_MODEL" not in reverted["constraint"]
        _migrate(url, "head")
        outputs = {}
        for mode in ("disabled", "selected", "failure", "budget"):
            outputs[mode] = asyncio.run(with_factory(partial(_exercise, mode=mode)))
        assert outputs["disabled"] == outputs["failure"] == outputs["budget"]
        retained = _snapshot(engine)
        with pytest.raises(DBAPIError, match="0056 downgrade refused"):
            _migrate(url, "0055_support_records", downgrade=True)
        assert _snapshot(engine) == retained
        assert retained["indexes"] == before["indexes"]
        assert set(before["rows"]).issubset(retained["rows"])
        print(f"isolated_database={name}; migration_path={path}; provider_requests=0")
    finally:
        engine.dispose()
