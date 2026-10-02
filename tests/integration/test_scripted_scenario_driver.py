from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.scenario_driver import ScriptedScenarioDriver
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import ScriptedGateway, _seed
from tests.integration.test_m7c_experiment_runner import db_factory, migrated_database

__all__ = ["db_factory", "migrated_database"]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]


@pytest.mark.asyncio
async def test_scenarios_use_real_turn_history_pinned_identity_and_independent_threads(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-{uuid4().hex}")
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run"}}
    )
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="Please provide your customer reference.", provider="fake", model="fake"
            ),
            ModelResponse(
                content="The customer reference is recorded.", provider="fake", model="fake"
            ),
        ]
    )
    runtime = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        thread_context_provider=SqlAlchemyThreadContextProvider(db_factory),
    )
    driver = ScriptedScenarioDriver(runtime, driver_kind="controlled")
    arguments = dict(
        case_id="clarification",
        agent_version_id=base["version"].id,
        user_turns=["Help with my account.", "My reference is local-fixture."],
        knowledge_snapshots=[],
    )
    first = await driver.execute(context, **arguments)
    second = await driver.execute(context, **arguments)
    assert first["thread_id"] != second["thread_id"]
    assert first["execution_complete"] and first["executed_turn_count"] == 2
    assert len({r["run_id"] for r in first["turns"] + second["turns"]}) == 4
    assert "local-fixture" not in str(first)
    assert "Please provide" in str(gateway.requests[1].messages)
    async with db_factory() as session:
        rows = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.thread_id.in_([first["thread_id"], second["thread_id"]])
                )
            )
        )
        assert len(rows) == 4
        assert all(row.agent_version_id == base["version"].id for row in rows)
        assert all(row.resolved_spec_hash == base["version"].resolved_spec_hash for row in rows)


@pytest.mark.asyncio
async def test_scenario_does_not_elevate_permissions_or_accept_another_workspace(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-auth-{uuid4().hex}")
    driver = ScriptedScenarioDriver(AgentRunService(db_factory), driver_kind="controlled")
    arguments = dict(
        case_id="scope",
        agent_version_id=base["version"].id,
        user_turns=["q"],
        knowledge_snapshots=[],
    )
    with pytest.raises(AgentHubError) as denied:
        await driver.execute(base["context"], **arguments)
    assert denied.value.status_code == 403
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run"}}
    )
    with pytest.raises(AgentHubError) as other:
        await driver.execute(context.model_copy(update={"workspace_id": str(uuid4())}), **arguments)
    assert other.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("decision, count", [("APPROVED", 1), ("DENIED", 0), (None, 0)])
async def test_scripted_approval_uses_persisted_decision_and_never_infers_consent(
    db_factory, decision, count
):
    spec = {
        "kind": "builtin",
        "identity": "create_ticket",
        "description": "Controlled ticket",
        "input_schema": {
            "type": "object",
            "properties": {"customer_ref": {"type": "string"}, "subject": {"type": "string"}},
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
    }
    async with db_factory() as session:
        base = await _seed(session, label=f"scenario-approval-{uuid4().hex}", tool_spec=spec)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="local", name="fixture")
        )
        await session.commit()
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="fake",
                tool_calls=(
                    ModelToolCall("create_ticket", {"customer_ref": "local", "subject": "fixture"}),
                ),
            ),
            ModelResponse(content="controlled final", provider="fake", model="fake"),
        ]
    )
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"evaluation_run", "approve_action"}}
    )
    runtime = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(db_factory),
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(os.environ["AGENTHUB_TEST_DATABASE_URL"]),
    )
    observation = await ScriptedScenarioDriver(runtime, driver_kind="controlled").execute(
        context,
        case_id="approval",
        agent_version_id=base["version"].id,
        user_turns=["Open a local fixture ticket."],
        knowledge_snapshots=[],
        approval_decisions=[decision] if decision else [],
    )
    async with db_factory() as session:
        actual = await session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.workspace_id == base["workspace_id"])
        )
        assert actual == count
    assert observation["approval_decision_count"] == (1 if decision else 0)
    assert observation["execution_complete"] is (decision is not None)
    if decision is None:
        assert observation["stop_reason"] == "approval_script_exhausted"
        assert observation["turns"][0]["status"] == "WAITING_APPROVAL"
