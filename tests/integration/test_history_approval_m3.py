"""A restarted approval resumes with the same frozen M3 history and transcript."""

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import create_checkpoint
from sqlalchemy import func, select

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.publish import _resolved_spec
from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.service import ThreadService
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
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
@pytest.mark.parametrize("changed_history", [False, True])
async def test_approval_resume_replays_same_source_and_complete_turn(db_factory, changed_history):
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
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    async with adapter.checkpointer() as checkpointer:
        checkpoint = await checkpointer.aget_tuple(
            adapter.config_for_run(base["workspace_id"], submitted.run_id)
        )
    assert checkpoint is not None
    binding = checkpoint.checkpoint["channel_values"]["history_checkpoint_binding"]
    assert binding["source_manifest_sha256"] == source_snapshot["source"]["manifest_sha256"]
    assert binding["run_id"] == str(submitted.run_id)
    approval = (await ApprovalService(db_factory).list(context))[0]
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    if changed_history:
        async with db_factory() as session:
            run = await session.scalar(select(AgentRun).where(AgentRun.id == submitted.run_id))
            snapshot = dict(run.effective_thread_history_snapshot)
            plan = dict(snapshot["plan"])
            plan["planned_tokens"] += 1
            snapshot["plan"] = plan
            run.effective_thread_history_snapshot = snapshot
            await session.commit()
        with pytest.raises(AgentHubError) as invalid:
            await service().resume(context, run_id=submitted.run_id, approval_id=approval.id)
        assert invalid.value.code == "AGENT_HISTORY_SNAPSHOT_INVALID"
        async with db_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Ticket)
                    .where(Ticket.workspace_id == base["workspace_id"])
                )
                == 0
            )
        assert gateway.calls == 1
        return
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


@pytest.mark.asyncio
async def test_resume_stops_before_action_when_validated_checkpoint_is_superseded(db_factory):
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

    class ReplacingCheckpointAdapter(LangGraphCheckpointAdapter):
        def __init__(self, database_url: str) -> None:
            super().__init__(database_url)
            self.workspace_id = None
            self.run_id = None
            self.resume_config: dict[str, Any] | None = None
            self.replacement_checkpoint_id: str | None = None
            self._replace_next_scope = False

        def config_for_checkpoint(
            self,
            workspace_id,
            run_id,
            *,
            checkpoint_id: str,
            checkpoint_ns: str,
        ):
            config = super().config_for_checkpoint(
                workspace_id,
                run_id,
                checkpoint_id=checkpoint_id,
                checkpoint_ns=checkpoint_ns,
            )
            self.resume_config = config
            self._replace_next_scope = True
            return config

        @asynccontextmanager
        async def checkpointer(self):
            async with super().checkpointer() as checkpointer:
                if self._replace_next_scope:
                    self._replace_next_scope = False
                    latest = await checkpointer.aget_tuple(
                        self.config_for_run(self.workspace_id, self.run_id)
                    )
                    assert latest is not None
                    replacement = create_checkpoint(
                        latest.checkpoint,
                        None,
                        int(latest.metadata.get("step", 0)) + 1,
                    )
                    metadata = dict(latest.metadata)
                    metadata["source"] = "update"
                    await checkpointer.aput(
                        latest.config,
                        replacement,
                        metadata,
                        latest.checkpoint["channel_versions"],
                    )
                    self.replacement_checkpoint_id = replacement["id"]
                    replaced_latest = await checkpointer.aget_tuple(
                        self.config_for_run(self.workspace_id, self.run_id)
                    )
                    assert replaced_latest is not None
                    assert (
                        replaced_latest.config["configurable"]["checkpoint_id"]
                        == self.replacement_checkpoint_id
                    )
                yield checkpointer

    gateway = ScriptedApprovalGateway()
    adapter = ReplacingCheckpointAdapter(TEST_DATABASE_URL)

    class CountingActionRuntime:
        def __init__(self, delegate: ActionRuntime) -> None:
            self.delegate = delegate
            self.calls = 0

        async def execute(self, *args, **kwargs):
            self.calls += 1
            return await self.delegate.execute(*args, **kwargs)

    action_runtime = CountingActionRuntime(ActionRuntime(session_factory=db_factory))

    def service():
        return AgentRunService(
            db_factory,
            model_gateway_factory=lambda session: gateway,
            approval_service=ApprovalService(db_factory),
            action_runtime=action_runtime,
            checkpoint_adapter=adapter,
            thread_context_provider=SqlAlchemyThreadContextProvider(db_factory),
        )

    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | {"workspace_read", "approve_action"}}
    )
    threads = ThreadService(db_factory, run_service=service())
    thread = await threads.create_thread(
        context, agent_id=base["version"].agent_id, title="checkpoint binding"
    )
    submitted = await threads.submit_turn(context, thread.id, user_input="Open controlled ticket")
    async with adapter.checkpointer() as checkpointer:
        checkpoint = await checkpointer.aget_tuple(
            adapter.config_for_run(base["workspace_id"], submitted.run_id)
        )
    assert checkpoint is not None
    checkpoint_configurable = checkpoint.config["configurable"]
    validated_checkpoint_id = checkpoint_configurable["checkpoint_id"]
    validated_checkpoint_ns = checkpoint_configurable["checkpoint_ns"]
    adapter.workspace_id = base["workspace_id"]
    adapter.run_id = submitted.run_id

    approval = (await ApprovalService(db_factory).list(context))[0]
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    with pytest.raises(AgentHubError) as invalid:
        await service().resume(context, run_id=submitted.run_id, approval_id=approval.id)
    assert invalid.value.code == "APPROVAL_CHECKPOINT_INVALID"
    assert action_runtime.calls == 0
    assert adapter.replacement_checkpoint_id is not None
    assert adapter.replacement_checkpoint_id != validated_checkpoint_id
    assert adapter.resume_config == {
        "configurable": {
            "thread_id": f"agenthub:{base['workspace_id']}:{submitted.run_id}",
            "checkpoint_ns": validated_checkpoint_ns,
            "checkpoint_id": validated_checkpoint_id,
        }
    }
    async with db_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.workspace_id == base["workspace_id"])
            )
            == 0
        )
