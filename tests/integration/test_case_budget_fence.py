"""Real database dispatch-fence checks; no provider is constructed."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from packages.agent_runtime.models import AgentRun
from packages.evaluation.case_budget_fence import lock_case_budget_dispatch
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.models import EvaluationExperimentCaseResult, EvaluationExperimentRun
from packages.model_gateway.run_budget_adapter import ModelAttemptBudgetError
from tests.integration.test_case_budget_ledger import (
    _create_budget_case,
)
from tests.integration.test_case_budget_ledger import (
    db_resources as db_resources,
)
from tests.integration.test_case_budget_ledger import (
    migrated_database as migrated_database,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip(),
        reason="Requires a dedicated case-budget test database.",
    ),
]

OWNER = "case-budget-test-agent-owner"


async def _dispatched(factory, monkeypatch):
    fixture = await _create_budget_case(factory, monkeypatch, turn_count=1)
    await InteractiveLifecycle(factory).dispatch(fixture.owned.identity, turn_index=0)
    async with factory() as session, session.begin():
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == fixture.turns[0].run.id)
            .values(
                execution_owner=OWNER,
                execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    return fixture


async def _lock(session, fixture, *, identity=None, owner=OWNER):
    return await lock_case_budget_dispatch(
        session,
        identity=identity or fixture.owned.identity,
        turn_index=0,
        agent_run_id=fixture.turns[0].run.id,
        execution_owner=owner,
    )


@pytest.mark.asyncio
async def test_active_dispatch_requires_both_evaluation_and_agent_owners(db_resources, monkeypatch):
    _, factory = db_resources
    fixture = await _dispatched(factory, monkeypatch)
    async with factory() as session, session.begin():
        assert (await _lock(session, fixture)).id == fixture.turns[0].run.id
    async with factory() as session, session.begin():
        with pytest.raises(ModelAttemptBudgetError, match="no longer current"):
            await _lock(session, fixture, owner="different-agent-owner")
    async with factory() as session, session.begin():
        with pytest.raises(ModelAttemptBudgetError, match="no longer current"):
            await _lock(
                session,
                fixture,
                identity=replace(
                    fixture.owned.identity, generation=fixture.owned.identity.generation + 1
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    ["evaluation_expired", "evaluation_cancelled", "case_owner", "agent_expired"],
)
async def test_stale_dispatch_fails_before_budget_reservation(db_resources, monkeypatch, changed):
    _, factory = db_resources
    fixture = await _dispatched(factory, monkeypatch)
    identity = fixture.owned.identity
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with factory() as session, session.begin():
        if changed.startswith("evaluation"):
            values = (
                {"lease_expires_at": past}
                if changed == "evaluation_expired"
                else {"status": "CANCEL_REQUESTED"}
            )
            await session.execute(
                update(EvaluationExperimentRun)
                .where(EvaluationExperimentRun.id == identity.run_id)
                .values(**values)
            )
        elif changed == "case_owner":
            await session.execute(
                update(EvaluationExperimentCaseResult)
                .where(EvaluationExperimentCaseResult.id == identity.case_id)
                .values(lease_owner="replacement-case-owner")
            )
        else:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == fixture.turns[0].run.id)
                .values(execution_lease_expires_at=past)
            )
    async with factory() as session, session.begin():
        with pytest.raises(ModelAttemptBudgetError) as error:
            await _lock(session, fixture)
        assert error.value.code == "CASE_BUDGET_EXECUTION_FENCE_INVALID"


@pytest.mark.asyncio
async def test_cached_agent_run_cannot_authorize_replaced_execution_owner(
    db_resources, monkeypatch
):
    _, factory = db_resources
    fixture = await _dispatched(factory, monkeypatch)
    async with factory() as session:
        cached = await session.scalar(
            select(AgentRun).where(AgentRun.id == fixture.turns[0].run.id)
        )
        assert cached.execution_owner == OWNER
        async with factory() as replacement, replacement.begin():
            await replacement.execute(
                update(AgentRun)
                .where(AgentRun.id == cached.id)
                .values(execution_owner="replacement-agent-owner")
            )
        with pytest.raises(ModelAttemptBudgetError, match="no longer current"):
            await _lock(session, fixture)
        assert cached.execution_owner == "replacement-agent-owner"
