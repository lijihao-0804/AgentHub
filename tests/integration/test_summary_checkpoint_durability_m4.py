"""A summary dispatch cannot outrun the checkpoint it must verify."""

from __future__ import annotations

import asyncio
import json

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.summary_models import ThreadSummaryJob
from packages.agent_runtime.summary_service import SummaryJobService
from tests.integration.test_summary_dispatch_security_m4 import (
    _migrated_isolated_v4_database as _migrated_isolated_v4_database,
)
from tests.integration.test_summary_dispatch_security_m4 import _run_diagnostics, _summary_thread
from tests.integration.test_summary_dispatch_security_m4 import (
    isolated_v4_db_factory as isolated_v4_db_factory,
)
from tests.integration.test_summary_dispatch_security_m4 import pytestmark as pytestmark


@pytest.mark.asyncio
async def test_summary_dispatch_waits_for_its_checkpoint_commit(
    isolated_v4_db_factory, monkeypatch
):
    factory = isolated_v4_db_factory
    run_context = await _summary_thread(factory, label="checkpoint-durability")
    original_put = AsyncPostgresSaver.aput
    original_dispatch = SummaryJobService.mark_dispatched
    committed_ids: set[str] = set()
    delayed_ids: set[str] = set()
    witnessed_ids: list[str] = []

    async def slow_put(saver, config, checkpoint, metadata, new_versions):
        # Delay the real write, not its returned acknowledgement. This makes
        # the framework's async persistence race deterministic on a local DB.
        if checkpoint.get("channel_values", {}).get("summary_boundary") is not None:
            delayed_ids.add(checkpoint["id"])
            await asyncio.sleep(0.2)
        result = await original_put(saver, config, checkpoint, metadata, new_versions)
        committed_ids.add(checkpoint["id"])
        return result

    async def verify_commit(service, key, *, lease_token, now=None):
        async with factory() as session:
            job = await session.get(ThreadSummaryJob, key.job_id)
            assert job is not None
            assert job.checkpoint_id in committed_ids
            witnessed_ids.append(job.checkpoint_id)
        return await original_dispatch(service, key, lease_token=lease_token, now=now)

    monkeypatch.setattr(AsyncPostgresSaver, "aput", slow_put)
    monkeypatch.setattr(SummaryJobService, "mark_dispatched", verify_commit)
    submitted = await run_context["thread_service"].submit_turn(
        run_context["context"],
        run_context["thread"].id,
        user_input="RECENT-TURN-04: owner takeover must prevent this summary send.",
        client_token="checkpoint-commit-boundary",
    )
    async with factory() as session:
        run = await session.get(AgentRun, submitted.run_id)
        assert run is not None
        status = run.status
    if status != "SUCCEEDED":
        pytest.fail(
            json.dumps(await _run_diagnostics(factory, run_id=submitted.run_id), sort_keys=True)
        )
    assert delayed_ids
    assert len(witnessed_ids) == 1
    assert witnessed_ids[0] in delayed_ids
    assert len(run_context["provider"].summary_requests) == 1
    assert len(run_context["provider"].main_requests) == 4
