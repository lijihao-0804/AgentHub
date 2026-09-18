from __future__ import annotations

import os
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set AGENTHUB_TEST_DATABASE_URL to run M5 checkpoint integration tests.",
)


class CheckpointState(TypedDict, total=False):
    workspace_id: str
    run_id: str
    value: int


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_checkpoint_round_trip() -> None:
    database_url = TEST_DATABASE_URL
    adapter = LangGraphCheckpointAdapter(database_url)
    workspace_id = UUID("11111111-1111-1111-1111-111111111111")
    run_id = uuid4()

    async def step(state: CheckpointState) -> dict[str, int]:
        return {"value": state.get("value", 0) + 1}

    builder = StateGraph(CheckpointState)
    builder.add_node("step", step)
    builder.add_edge(START, "step")
    builder.add_edge("step", END)
    async with adapter.checkpointer() as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        result = await graph.ainvoke(
            {"workspace_id": str(workspace_id), "run_id": str(run_id), "value": 1},
            config=adapter.config_for_run(workspace_id, run_id),
        )
        assert result["value"] == 2
        checkpoint = await checkpointer.aget_tuple(adapter.config_for_run(workspace_id, run_id))
        assert checkpoint is not None
