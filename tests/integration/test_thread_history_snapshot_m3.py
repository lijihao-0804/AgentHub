"""Real database serialization of competing first history snapshots."""

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.core.errors.exceptions import AgentHubError
from tests.integration.test_m4d_streaming_budget import TEST_DATABASE_URL, _seed

pytest_plugins = ("tests.integration.test_m4d_streaming_budget",)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="Requires an isolated PostgreSQL test database"
    ),
]


@pytest.mark.asyncio
async def test_competing_prepares_install_exactly_one_history_manifest(db_factory):
    seed = await _seed(db_factory)
    async with db_factory() as session:
        run = AgentRun(
            workspace_id=seed["workspace_id"],
            agent_version_id=seed["version"].id,
            input_text="offline task",
            created_by=UUID(seed["context"].user_id),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    graphs = []
    for _ in range(2):
        graph = _AgentRunGraph.__new__(_AgentRunGraph)
        graph.service = SimpleNamespace(session_factory=db_factory)
        graph.run = SimpleNamespace(id=run_id, effective_thread_history_snapshot=None)
        graphs.append(graph)

    first = {"version": 1, "source_ids": ["first"]}
    second = {"version": 1, "source_ids": ["second"]}
    results = await asyncio.gather(
        graphs[0]._install_thread_history_snapshot(first, workspace_id=seed["workspace_id"]),
        graphs[1]._install_thread_history_snapshot(second, workspace_id=seed["workspace_id"]),
    )
    assert results[0] == results[1]
    assert results[0] in (first, second)
    async with db_factory() as session:
        stored = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        assert stored.effective_thread_history_snapshot == results[0]
    with pytest.raises(AgentHubError) as error:
        await graphs[0]._install_thread_history_snapshot(first, workspace_id=uuid4())
    assert error.value.code == "AGENT_RUN_NOT_FOUND"
