"""Usage remains cumulative across a new process's durable approval resume."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.model_gateway.contracts import CostEstimate, ModelUsage
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import (
    TEST_DATABASE_URL,
    ScriptedApprovalGateway,
)
from tests.integration.test_m5a_approval_runtime import (
    db_factory as db_factory,
)
from tests.integration.test_m5a_approval_runtime import (
    migrated_database as migrated_database,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="isolated DB required"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision", [ApprovalDecisionStatus.APPROVED, ApprovalDecisionStatus.DENIED]
)
async def test_restart_resume_preserves_usage_and_waiting_cost(db_factory, decision):
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
        "description": "Controlled ticket",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "subject": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="cust-1", name="synthetic")
        )
        await session.commit()
    gateway = ScriptedApprovalGateway()
    gateway.responses = [
        replace(
            r,
            usage=ModelUsage(
                input_tokens=100, output_tokens=10, total_tokens=110, cached_tokens=20
            ),
            cost_estimate=CostEstimate(Decimal("0.01"), "USD"),
        )
        for r in gateway.responses
    ]

    def service():
        return AgentRunService(
            db_factory,
            model_gateway_factory=lambda s: gateway,
            approval_service=ApprovalService(db_factory),
            action_runtime=ActionRuntime(session_factory=db_factory),
            checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
        )

    ctx = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"workspace_read", "approve_action"}}
    )
    first = await service().run(
        ctx, agent_version_id=base["version"].id, input_text="Open controlled ticket"
    )
    assert first.status == "WAITING_APPROVAL"
    assert first.total_input_tokens == 100
    assert first.total_cost_amount == Decimal("0.01")
    approval = (await ApprovalService(db_factory).list(ctx))[0]
    await ApprovalService(db_factory).decide(ctx, approval.id, decision=decision)
    result = await service().resume(ctx, run_id=first.run_id, approval_id=approval.id)
    assert result.status == "SUCCEEDED"
    assert result.total_input_tokens == 200
    assert result.total_output_tokens == 20
    assert result.total_cached_tokens == 40
    assert result.total_cost_amount == Decimal("0.02")
    again = await service().resume(ctx, run_id=first.run_id, approval_id=approval.id)
    assert again.total_cost_amount == Decimal("0.02")
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_resumed_cost_guard_uses_checkpoint_spend(db_factory):
    tool_spec = {
        "kind": "builtin",
        "identity": "create_ticket",
        "description": "Controlled ticket",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "subject": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        spec = dict(base["version"].resolved_spec)
        spec["runtime"] = {**spec.get("runtime", {}), "max_cost_micro_usd": 5000}
        base["version"].resolved_spec = spec
        base["version"].resolved_spec_hash = canonical_json_hash(spec)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="cust-1", name="synthetic")
        )
        await session.commit()
    gateway = ScriptedApprovalGateway()
    gateway.responses = [
        replace(
            r,
            usage=ModelUsage(input_tokens=100, output_tokens=10, total_tokens=110),
            cost_estimate=CostEstimate(Decimal("0.01"), "USD"),
        )
        for r in gateway.responses
    ]

    def service():
        return AgentRunService(
            db_factory,
            model_gateway_factory=lambda s: gateway,
            approval_service=ApprovalService(db_factory),
            action_runtime=ActionRuntime(session_factory=db_factory),
            checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
        )

    ctx = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"workspace_read", "approve_action"}}
    )
    first = await service().run(ctx, agent_version_id=base["version"].id, input_text="Open ticket")
    approval = (await ApprovalService(db_factory).list(ctx))[0]
    await ApprovalService(db_factory).decide(
        ctx, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    result = await service().resume(ctx, run_id=first.run_id, approval_id=approval.id)
    assert result.status == "NEEDS_ATTENTION"
    assert result.failure_code == "AGENT_COST_LIMIT_EXCEEDED"
    assert result.total_cost_amount == Decimal("0.01")
    assert gateway.calls == 1
