from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime_graph import _AgentRunGraph


class _Session:
    def __init__(self, row) -> None:
        self.row = row

    async def scalar(self, _statement):
        return self.row


class _SessionContext:
    def __init__(self, row) -> None:
        self.session = _Session(row)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc_info):
        return None


@pytest.mark.asyncio
async def test_summary_maps_real_run_lease_agenthub_error_to_needs_attention() -> None:
    run_row = SimpleNamespace(
        status="RUNNING",
        execution_owner="replacement-owner",
        execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    graph = _AgentRunGraph(
        SimpleNamespace(session_factory=lambda: _SessionContext(run_row)),
        SimpleNamespace(workspace_id=str(uuid4())),
        SimpleNamespace(id=uuid4()),
        execution_owner_token="original-owner",
    )
    steps = []

    async def record_step(kind, status, safe_metadata=None):
        steps.append((kind, status, safe_metadata))

    graph.step = record_step
    state = {
        "runtime": {"summary_policy": {"enabled": True}},
        "history_checkpoint_binding": {"present": True},
    }

    result = await graph.summary(state, config={})

    assert result == {
        "failure_code": "RUN_EXECUTION_LEASE_LOST",
        "run_status": "NEEDS_ATTENTION",
    }
    assert steps == [
        ("SUMMARY", "FAILED", {"error_code": "RUN_EXECUTION_LEASE_LOST"})
    ]
