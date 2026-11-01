from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.core.errors.exceptions import AgentHubError


def _graph(owner_token: str | None) -> _AgentRunGraph:
    return _AgentRunGraph(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=uuid4()),
        execution_owner_token=owner_token,
    )


def _run_row(*, owner: str, expires_at: datetime | None = None):
    return SimpleNamespace(
        status="RUNNING",
        execution_owner=owner,
        execution_lease_expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_summary_lease_requires_the_graphs_exact_owner_token() -> None:
    graph = _graph("current-executor")

    with pytest.raises(AgentHubError) as rejected:
        await graph._assert_summary_run_lease(
            run_row=_run_row(owner="different-live-executor")
        )

    assert rejected.value.code == "RUN_EXECUTION_LEASE_LOST"


@pytest.mark.asyncio
async def test_summary_lease_rejects_missing_or_expired_owner_tokens() -> None:
    live_row = _run_row(owner="stored-owner")
    no_token_graph = _graph(None)
    expired_graph = _graph("stored-owner")
    expired_row = _run_row(
        owner="stored-owner",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    for graph, row in ((no_token_graph, live_row), (expired_graph, expired_row)):
        with pytest.raises(AgentHubError) as rejected:
            await graph._assert_summary_run_lease(run_row=row)
        assert rejected.value.code == "RUN_EXECUTION_LEASE_LOST"


@pytest.mark.asyncio
async def test_summary_lease_accepts_only_the_matching_active_owner() -> None:
    graph = _graph("current-executor")
    row = _run_row(owner="current-executor")

    assert await graph._assert_summary_run_lease(run_row=row) is None
