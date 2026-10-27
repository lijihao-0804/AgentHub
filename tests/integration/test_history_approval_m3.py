"""A restarted approval resumes with the same frozen M3 history and transcript."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.publish import _resolved_spec
from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.service import ThreadService
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import TEST_DATABASE_URL, ScriptedApprovalGateway
from tests.integration.test_m5a_approval_runtime import db_factory as db_factory
from tests.integration.test_m5a_approval_runtime import migrated_database as migrated_database
from tests.unit.test_agent_publish import _agent
from tests.unit.test_context_policy_m2_runtime import _profile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="isolated DB required"),
]


@pytest.mark.asyncio
async def test_approval_resume_replays_same_source_and_complete_turn(db_factory):
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
        "timeout_seconds": 30,
    }
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=tool_spec)
        session.add(Customer(workspace_id=base["workspace_id"], customer_ref="cust-1", name="test"))
        profile = replace(
            _profile(total=32768, output=256, workspace=base["workspace_id"]),
            id=base["profile"].id,
            provider_credential_id=base["credential"].id,
        )
        draft = _agent(
            workspace_id=base["workspace_id"],
            model_profile_id=profile.id,
            runtime_config={
                "context_policy": {"policy_version": "adaptive_context_v1"},
                "history_policy": "deterministic_history_v1",
            },
        )
        old = base["version"].resolved_spec
        spec = _resolved_spec(
            agent=draft,
            model=((profile,), {profile.id: "fake"}),
            bindings=(),
            tools=tuple(old["tools"]),
        )
        base["version"].spec_schema_version = 4
        base["version"].resolved_spec = spec
        base["version"].resolved_spec_hash = canonical_json_hash(spec)
        await session.commit()

    class RecordingGateway(ScriptedApprovalGateway):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def generate_resolved(self, context, plan, request):
            self.requests.append(request)
            return await super().generate_resolved(context, plan, request)

    gateway = RecordingGateway()

    def service():
        return AgentRunService(
            db_factory,
            model_gateway_factory=lambda session: gateway,
            approval_service=ApprovalService(db_factory),
            action_runtime=ActionRuntime(session_factory=db_factory),
            checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
            thread_context_provider=SqlAlchemyThreadContextProvider(db_factory),
        )

    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"workspace_read", "approve_action"}}
    )
    threads = ThreadService(db_factory, run_service=service())
    thread = await threads.create_thread(
        context, agent_id=base["version"].agent_id, title="approval history"
    )
    submitted = await threads.submit_turn(context, thread.id, user_input="Open controlled ticket")
    async with db_factory() as session:
        waiting = await session.scalar(select(AgentRun).where(AgentRun.id == submitted.run_id))
        assert waiting.status == "WAITING_APPROVAL"
        source_snapshot = waiting.effective_thread_history_snapshot
    approval = (await ApprovalService(db_factory).list(context))[0]
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    result = await service().resume(context, run_id=submitted.run_id, approval_id=approval.id)
    assert result.status == "SUCCEEDED"
    async with db_factory() as session:
        completed = await session.scalar(select(AgentRun).where(AgentRun.id == submitted.run_id))
        assert completed.effective_thread_history_snapshot == source_snapshot
        assert [item["role"] for item in completed.thread_history_transcript["messages"]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
    assert gateway.calls == 2
    next_turn = await threads.submit_turn(context, thread.id, user_input="What happened?")
    async with db_factory() as session:
        following = await session.scalar(select(AgentRun).where(AgentRun.id == next_turn.run_id))
        assert following.status == "SUCCEEDED"
    assert gateway.calls == 3
    replay = gateway.requests[-1].messages
    historical_call = next(message for message in replay if message.tool_calls)
    historical_result = next(message for message in replay if message.role == "tool")
    assert historical_result.tool_call_id == historical_call.tool_calls[0].provider_tool_call_id
