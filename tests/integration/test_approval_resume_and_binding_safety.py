"""Real PostgreSQL coverage for approval resume and Turn binding safety."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
)
from packages.agent_runtime.models import Agent, AgentRun, RunStep
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.approvals import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
    ApprovalService,
)
from packages.control_plane.models import (
    OrganizationMembership,
    Workspace,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.threads.models import AgentThread, ThreadTurn
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import (
    ScriptedApprovalGateway,
)
from tests.integration.test_m5a_approval_runtime import (
    db_factory as db_factory,
)
from tests.integration.test_m5a_approval_runtime import (
    migrated_database as migrated_database,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="isolated PostgreSQL required"),
]


def _tool_spec(*, identity: str = "create_ticket") -> dict[str, object]:
    return {
        "kind": "builtin",
        "identity": identity,
        "description": "Controlled action",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["customer_ref", "subject"],
            "additionalProperties": False,
        },
        "effect": "WRITE",
        "risk_level": "HIGH",
        "approval_policy": "ALWAYS",
    }


async def _seed_approval(factory, *, identity: str = "create_ticket"):
    async with factory() as session:
        base = await _seed(
            session,
            label=uuid4().hex,
            tool_spec=_tool_spec(identity=identity),
            model="fake-approval",
        )
        session.add(Customer(workspace_id=base["workspace_id"], customer_ref="cust", name="Safety"))
        await session.commit()
    gateway = ScriptedApprovalGateway()
    gateway.responses[0] = ModelResponse(
        content="",
        provider="fake",
        model="fake-approval",
        tool_calls=(
            ModelToolCall(
                name="create_ticket",
                arguments={"customer_ref": "cust", "subject": "controlled action"},
                provider_tool_call_id="provider-call",
            ),
        ),
    )
    if identity != "create_ticket":
        gateway.responses = [
            ModelResponse(
                content="",
                provider="fake",
                model="fake-approval",
                tool_calls=(
                    ModelToolCall(
                        name=identity,
                        arguments={"customer_ref": "cust", "subject": "knowledge"},
                        provider_tool_call_id="provider-call",
                    ),
                ),
            ),
            ModelResponse(content="done", provider="fake", model="fake-approval"),
        ]
    return base, gateway


def _service(factory, gateway):
    return AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=ApprovalService(factory),
        action_runtime=ActionRuntime(session_factory=factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )


async def _waiting_approval(factory, base, gateway):
    context = base["context"].model_copy(
        update={
            "permissions": frozenset(
                {
                    "agent_run",
                    "workspace_read",
                    "tool_run",
                    "knowledge_run",
                    "approve_action",
                }
            )
        }
    )
    service = _service(factory, gateway)
    first = await service.run(
        context, agent_version_id=base["version"].id, input_text="controlled action"
    )
    assert first.status == "WAITING_APPROVAL", (first.status, first.failure_code)
    approval = (await ApprovalService(factory).list(context))[0]
    return context, service, first, approval


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["workspace_read", "tool_run", "knowledge_run"])
async def test_resume_rechecks_current_actor_permissions_before_claim(
    db_factory, monkeypatch, revoked
) -> None:
    identity = "search_knowledge" if revoked == "knowledge_run" else "create_ticket"
    base, gateway = await _seed_approval(db_factory, identity=identity)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    revoked_permissions = frozenset(
        {"agent_run", "workspace_read", "tool_run", "knowledge_run", "approve_action"}
    ) - {revoked}

    async def authoritative(_request_context, _run):
        return context.model_copy(update={"permissions": revoked_permissions})

    monkeypatch.setattr(service, "_authoritative_run_context", authoritative)

    result = await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    assert result.status == "NEEDS_ATTENTION"
    assert result.failure_code == "RUN_ACTOR_ACCESS_REVOKED"
    async with db_factory() as session:
        run = await session.get(AgentRun, waiting.run_id)
        stored = await session.get(type(approval), approval.id)
    assert run is not None and stored is not None
    assert run.status == "NEEDS_ATTENTION"
    assert stored.execution_status == "NOT_STARTED"
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_resume_revoked_membership_uses_real_authoritative_lookup(
    db_factory,
) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    async with db_factory() as session:
        run = await session.get(AgentRun, waiting.run_id)
        assert run is not None
        workspace = await session.get(Workspace, base["workspace_id"])
        assert workspace is not None
        membership = await session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == base["user"].id,
                OrganizationMembership.organization_id == workspace.organization_id,
            )
        )
        assert membership is not None
        await session.delete(membership)
        await session.commit()

    result = await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    assert (result.status, result.failure_code) == (
        "NEEDS_ATTENTION",
        "RUN_ACTOR_ACCESS_REVOKED",
    )
    async with db_factory() as session:
        run = await session.get(AgentRun, waiting.run_id)
        stored = await session.get(type(approval), approval.id)
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert run is not None and stored is not None
    assert (run.status, run.failure_code) == ("NEEDS_ATTENTION", "RUN_ACTOR_ACCESS_REVOKED")
    assert stored.execution_status == "NOT_STARTED"
    assert tickets == []


@pytest.mark.asyncio
async def test_checkpoint_interrupt_approval_mismatch_fails_closed(
    db_factory,
) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )

    class MismatchedCheckpoint:
        def __init__(self, original):
            self._original = original

        async def aget_tuple(self, *args, **kwargs):
            checkpoint = await self._original.aget_tuple(*args, **kwargs)
            assert checkpoint is not None
            pending = []
            for write in checkpoint.pending_writes:
                channel, value = write[1], write[2]
                if channel == "__interrupt__":
                    items = value if isinstance(value, (list, tuple)) else (value,)
                    pending.append(
                        (
                            write[0],
                            channel,
                            tuple(
                                SimpleNamespace(value={**item.value, "approval_id": str(uuid4())})
                                for item in items
                            ),
                        )
                    )
                else:
                    pending.append(write)
            return SimpleNamespace(
                pending_writes=tuple(pending),
                config=getattr(checkpoint, "config", None),
                metadata=getattr(checkpoint, "metadata", None),
            )

    checkpoint_adapter = service.checkpoint_adapter
    original_checkpointer = checkpoint_adapter.checkpointer

    class Adapter:
        def __getattr__(self, name):
            return getattr(checkpoint_adapter, name)

        def checkpointer(self):
            manager = original_checkpointer()

            class Manager:
                async def __aenter__(self):
                    return MismatchedCheckpoint(await manager.__aenter__())

                async def __aexit__(self, *args):
                    return await manager.__aexit__(*args)

            return Manager()

    service.checkpoint_adapter = Adapter()
    with pytest.raises(AgentHubError, match="checkpoint is invalid"):
        await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    async with db_factory() as session:
        run = await session.get(AgentRun, waiting.run_id)
        stored = await session.get(type(approval), approval.id)
    assert run is not None and stored is not None
    assert (run.status, run.failure_code) == (
        "NEEDS_ATTENTION",
        "APPROVAL_RESUME_MISMATCH",
    )
    assert stored.execution_status == "NOT_STARTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["workspace_read", "tool_run", "knowledge_run"])
async def test_permission_change_during_resume_is_checked_before_claim(
    db_factory, monkeypatch, revoked
) -> None:
    identity = "search_knowledge" if revoked == "knowledge_run" else "create_ticket"
    base, gateway = await _seed_approval(db_factory, identity=identity)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    calls = 0

    async def narrowed(_request_context, _run):
        nonlocal calls
        calls += 1
        if calls < 3:
            return context
        return context.model_copy(update={"permissions": context.permissions - {revoked}})

    monkeypatch.setattr(service, "_authoritative_run_context", narrowed)
    result = await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    assert result.status == "NEEDS_ATTENTION"
    assert result.failure_code == "RUN_ACTOR_ACCESS_REVOKED"
    async with db_factory() as session:
        stored = await session.get(type(approval), approval.id)
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert stored is not None
    assert calls == 3
    assert stored.execution_status == "NOT_STARTED"
    assert tickets == []
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_permission_revoked_after_claim_prevents_action_execution(db_factory, monkeypatch):
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    claimed = False
    original_claim = service.approval_service.claim_execution

    async def claim_then_revoke(*args, **kwargs):
        nonlocal claimed
        result = await original_claim(*args, **kwargs)
        claimed = result is not None
        return result

    async def current_context(_request_context, _run):
        if claimed:
            return context.model_copy(update={"permissions": context.permissions - {"tool_run"}})
        return context

    monkeypatch.setattr(service.approval_service, "claim_execution", claim_then_revoke)
    monkeypatch.setattr(service, "_authoritative_run_context", current_context)
    result = await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    assert result.status == "NEEDS_ATTENTION"
    async with db_factory() as session:
        stored = await session.get(type(approval), approval.id)
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert claimed and stored is not None and stored.execution_status == "CLAIMED"
    assert tickets == []
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_unknown_outcome_replays_with_agent_run_permission_only(
    db_factory, monkeypatch
) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    await service.approval_service.claim_execution(context, approval.id)
    await service.approval_service.complete_execution(
        context,
        approval.id,
        status=ApprovalExecutionStatus.UNKNOWN_OUTCOME,
        failure_code="ACTION_OUTCOME_UNKNOWN",
        safe_failure_message="Outcome needs reconciliation.",
    )
    executor_calls = 0

    class NeverExecute:
        async def execute(self, *_args, **_kwargs):
            nonlocal executor_calls
            executor_calls += 1
            raise AssertionError("a terminal unknown action must not execute again")

    service.action_runtime = NeverExecute()

    async def replay_context(_request_context, _run):
        return context.model_copy(update={"permissions": frozenset({"agent_run"})})

    monkeypatch.setattr(service, "_authoritative_run_context", replay_context)
    result = await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    assert (result.status, result.failure_code) == (
        "NEEDS_ATTENTION",
        "ACTION_RECONCILIATION_REQUIRED",
    )
    assert executor_calls == 0
    async with db_factory() as session:
        stored = await session.get(type(approval), approval.id)
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert stored is not None
    assert stored.execution_status == "UNKNOWN_OUTCOME"
    assert tickets == []


@pytest.mark.asyncio
async def test_resume_graph_failure_converges_running_to_needs_attention(
    db_factory, monkeypatch
) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )

    async def crash(*_args, **_kwargs):
        raise RuntimeError("crash after RUNNING")

    monkeypatch.setattr("packages.agent_runtime.runtime_service._AgentRunGraph.invoke", crash)
    with pytest.raises(RuntimeError, match="crash after RUNNING"):
        await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    async with db_factory() as session:
        run = await session.get(AgentRun, waiting.run_id)
        stored = await session.get(type(approval), approval.id)
    assert run is not None and stored is not None
    assert run.status == "NEEDS_ATTENTION"
    assert run.failure_code in {"RUN_EXECUTION_INTERRUPTED", "ACTION_RECONCILIATION_REQUIRED"}
    assert stored.execution_status == "NOT_STARTED"


async def _thread(factory):
    async with factory() as session:
        base = await _seed(session, label=uuid4().hex)
        thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title="binding safety",
            created_by=base["user"].id,
        )
        session.add(thread)
        await session.commit()
    return base, thread


@pytest.mark.asyncio
async def test_prepare_run_requires_complete_matching_turn_binding(db_factory) -> None:
    base, thread = await _thread(db_factory)
    async with db_factory() as session:
        turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=1,
            user_input="bound input",
            client_token=uuid4().hex,
            input_hash="a" * 64,
        )
        session.add(turn)
        await session.commit()
    service = AgentRunService(db_factory)

    for kwargs in (
        {"thread_id": thread.id},
        {"thread_turn_id": turn.id},
        {"thread_id": thread.id, "thread_turn_id": turn.id},
    ):
        input_text = "different input" if len(kwargs) == 2 else turn.user_input
        with pytest.raises(AgentHubError) as raised:
            await service.prepare_run(
                base["context"],
                agent_version_id=base["version"].id,
                input_text=input_text,
                **kwargs,
            )
        assert raised.value.code == "THREAD_TURN_BINDING_INVALID"
    async with db_factory() as session:
        assert not list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == base["workspace_id"],
                    AgentRun.thread_id == thread.id,
                )
            )
        )


@pytest.mark.asyncio
async def test_prepare_run_rejects_foreign_legacy_and_mismatched_bound_run(
    db_factory,
) -> None:
    base, thread = await _thread(db_factory)
    async with db_factory() as session:
        other_thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title="other thread",
            created_by=base["user"].id,
        )
        session.add(other_thread)
        await session.flush()
        foreign_turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=other_thread.id,
            sequence=1,
            user_input="foreign turn",
            client_token=uuid4().hex,
            input_hash="c" * 64,
        )
        legacy_turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=1,
            user_input="legacy turn",
            client_token=uuid4().hex,
            input_hash="d" * 64,
            legacy_recovery_required=True,
        )
        session.add_all((foreign_turn, legacy_turn))
        await session.commit()

    service = AgentRunService(db_factory)
    for turn, supplied_thread, input_text in (
        (foreign_turn, thread.id, foreign_turn.user_input),
        (legacy_turn, thread.id, legacy_turn.user_input),
    ):
        with pytest.raises(AgentHubError) as raised:
            await service.prepare_run(
                base["context"],
                agent_version_id=base["version"].id,
                input_text=input_text,
                thread_id=supplied_thread,
                thread_turn_id=turn.id,
            )
        assert raised.value.code == "THREAD_TURN_BINDING_INVALID"

    async with db_factory() as session:
        turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=2,
            user_input="bound stable run",
            client_token=uuid4().hex,
            input_hash="e" * 64,
        )
        session.add(turn)
        await session.commit()
    run = await service.prepare_run(
        base["context"],
        agent_version_id=base["version"].id,
        input_text=turn.user_input,
        thread_id=thread.id,
        thread_turn_id=turn.id,
    )
    for version_id, input_text in (
        (uuid4(), turn.user_input),
        (base["version"].id, "changed input"),
    ):
        with pytest.raises(AgentHubError) as raised:
            await service.prepare_run(
                base["context"],
                agent_version_id=version_id,
                input_text=input_text,
                thread_id=thread.id,
                thread_turn_id=turn.id,
            )
        assert raised.value.code == "THREAD_TURN_BINDING_INVALID"
    async with db_factory() as session:
        persisted = list(
            await session.scalars(select(AgentRun).where(AgentRun.thread_turn_id == turn.id))
        )
    assert [item.id for item in persisted] == [run.id]


@pytest.mark.asyncio
async def test_prepare_run_rejects_crossed_bindings_and_foreign_agent(
    db_factory,
) -> None:
    base, thread = await _thread(db_factory)
    async with db_factory() as session:
        other_thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title="binding mismatch",
            created_by=base["user"].id,
        )
        session.add(other_thread)
        await session.flush()
        turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=1,
            user_input="crossed binding",
            client_token=uuid4().hex,
            input_hash="1" * 64,
        )
        other_run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            thread_id=other_thread.id,
            input_text="other run",
            created_by=base["user"].id,
        )
        session.add_all((turn, other_run))
        await session.flush()
        turn.agent_run_id = other_run.id
        await session.commit()

    service = AgentRunService(db_factory)
    with pytest.raises(AgentHubError) as raised:
        await service.prepare_run(
            base["context"],
            agent_version_id=base["version"].id,
            input_text=turn.user_input,
            thread_id=thread.id,
            thread_turn_id=turn.id,
        )
    assert raised.value.code == "THREAD_TURN_BINDING_INVALID"

    async with db_factory() as session:
        turn.agent_run_id = None
        stable = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            thread_id=thread.id,
            thread_turn_id=turn.id,
            input_text=turn.user_input,
            created_by=base["user"].id,
        )
        foreign_agent = Agent(
            workspace_id=base["workspace_id"],
            name=f"foreign-{uuid4().hex}",
            system_prompt="foreign agent",
            model_profile_id=base["profile"].id,
        )
        session.add_all((stable, foreign_agent))
        await session.flush()
        turn.agent_run_id = other_run.id
        await session.commit()

    with pytest.raises(AgentHubError) as raised:
        await service.prepare_run(
            base["context"],
            agent_version_id=base["version"].id,
            input_text=turn.user_input,
            thread_id=thread.id,
            thread_turn_id=turn.id,
        )
    assert raised.value.code == "THREAD_TURN_BINDING_INVALID"


@pytest.mark.asyncio
async def test_finish_retry_reuses_committed_terminal_step(
    db_factory,
) -> None:
    base, gateway = await _seed_approval(db_factory)
    service = AgentRunService(db_factory)
    run = await service.prepare_run(
        base["context"],
        agent_version_id=base["version"].id,
        input_text="retry finish",
    )
    pending_finish = {
        "status": "SUCCEEDED",
        "safe_metadata": {"status": "SUCCEEDED", "error_code": None},
    }
    for _ in range(2):
        result = await service._complete_run(
            base["context"],
            run.id,
            status="SUCCEEDED",
            final_output="committed once",
            failure_code=None,
            model_step_count=1,
            tool_call_count=0,
            usage_records=[],
            pending_finish=pending_finish,
        )
    assert (result.status, result.final_output) == ("SUCCEEDED", "committed once")
    async with db_factory() as session:
        steps = list(
            await session.scalars(
                select(RunStep).where(
                    RunStep.workspace_id == base["workspace_id"],
                    RunStep.agent_run_id == run.id,
                    RunStep.kind == "FINISH",
                )
            )
        )
    assert len(steps) == 1


@pytest.mark.asyncio
async def test_prepare_run_does_not_swallow_unrelated_integrity_error(
    db_factory, monkeypatch
) -> None:
    base, thread = await _thread(db_factory)
    async with db_factory() as session:
        turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=1,
            user_input="unrelated constraint",
            client_token=uuid4().hex,
            input_hash="f" * 64,
        )
        session.add(turn)
        await session.commit()
    service = AgentRunService(db_factory)
    original_commit = AsyncSession.commit
    calls = 0
    failure = IntegrityError(
        "INSERT INTO agent_runs",
        {},
        RuntimeError('duplicate key violates "uq_agent_runs_other"'),
    )

    async def failing_commit(session):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    with pytest.raises(IntegrityError) as raised:
        await service.prepare_run(
            base["context"],
            agent_version_id=base["version"].id,
            input_text=turn.user_input,
            thread_id=thread.id,
            thread_turn_id=turn.id,
        )
    assert raised.value is failure
    assert calls == 1
    monkeypatch.setattr(AsyncSession, "commit", original_commit)


@pytest.mark.asyncio
async def test_concurrent_prepare_same_turn_returns_one_run(db_factory) -> None:
    base, thread = await _thread(db_factory)
    async with db_factory() as session:
        turn = ThreadTurn(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            sequence=1,
            user_input="same concurrent input",
            client_token=uuid4().hex,
            input_hash="b" * 64,
        )
        session.add(turn)
        await session.commit()
    service = AgentRunService(db_factory)

    async def prepare():
        return await service.prepare_run(
            base["context"],
            agent_version_id=base["version"].id,
            input_text=turn.user_input,
            thread_id=thread.id,
            thread_turn_id=turn.id,
        )

    first, second = await asyncio.gather(prepare(), prepare())
    assert first.id == second.id
    async with db_factory() as session:
        runs = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.workspace_id == base["workspace_id"],
                    AgentRun.thread_turn_id == turn.id,
                )
            )
        )
    assert [run.id for run in runs] == [first.id]


@pytest.mark.asyncio
async def test_finish_and_run_terminal_state_commit_together_after_commit_failure(
    db_factory, monkeypatch
) -> None:
    base, gateway = await _seed_approval(db_factory)
    run = await AgentRunService(db_factory).prepare_run(
        base["context"],
        agent_version_id=base["version"].id,
        input_text="atomic finish input",
    )
    service = AgentRunService(db_factory)
    original_commit = AsyncSession.commit
    calls = 0
    failure = RuntimeError("force terminal commit failure")

    async def fail_target_commit(session):
        nonlocal calls
        calls += 1
        await session.flush()
        raise failure

    monkeypatch.setattr(AsyncSession, "commit", fail_target_commit)
    with pytest.raises(RuntimeError, match="force terminal commit failure"):
        await service._complete_run(
            base["context"],
            run.id,
            status="SUCCEEDED",
            final_output="atomic answer",
            failure_code=None,
            model_step_count=1,
            tool_call_count=0,
            usage_records=[],
            pending_finish={
                "status": "SUCCEEDED",
                "safe_metadata": {"status": "SUCCEEDED", "error_code": None},
            },
        )
    assert calls == 1
    await asyncio.sleep(0)
    async with db_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        finish_steps = list(
            await session.scalars(
                select(RunStep).where(
                    RunStep.workspace_id == base["workspace_id"],
                    RunStep.agent_run_id == run.id,
                    RunStep.kind == "FINISH",
                )
            )
        )
    assert persisted is not None
    assert (persisted.status, persisted.completed_at, persisted.final_output) == (
        "RUNNING",
        None,
        None,
    )
    assert finish_steps == []

    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    result = await service._complete_run(
        base["context"],
        run.id,
        status="SUCCEEDED",
        final_output="atomic answer",
        failure_code=None,
        model_step_count=1,
        tool_call_count=0,
        usage_records=[],
        pending_finish={
            "status": "SUCCEEDED",
            "safe_metadata": {"status": "SUCCEEDED", "error_code": None},
        },
    )
    assert (result.status, result.final_output) == ("SUCCEEDED", "atomic answer")
    async with db_factory() as session:
        persisted = await session.get(AgentRun, run.id)
        finish_steps = list(
            await session.scalars(
                select(RunStep).where(
                    RunStep.workspace_id == base["workspace_id"],
                    RunStep.agent_run_id == run.id,
                    RunStep.kind == "FINISH",
                )
            )
        )
    assert persisted is not None
    assert persisted.status == "SUCCEEDED"
    assert persisted.completed_at is not None
    assert len(finish_steps) == 1
    assert finish_steps[0].status == persisted.status
    assert finish_steps[0].safe_metadata["status"] == persisted.status
    assert finish_steps[0].safe_metadata["error_code"] == persisted.failure_code


@pytest.mark.asyncio
async def test_restart_after_terminal_commit_failure_recovers_finish(
    db_factory, monkeypatch
) -> None:
    """A fresh graph must not commit Run terminal state without FINISH."""

    base, gateway = await _seed_approval(db_factory)
    gateway.responses = [
        ModelResponse(content="restart-safe answer", provider="fake", model="fake-approval")
    ]
    service = AgentRunService(db_factory, model_gateway_factory=lambda session: gateway)
    run = await service.prepare_run(
        base["context"],
        agent_version_id=base["version"].id,
        input_text="restart safe",
    )
    first_graph = _AgentRunGraph(service, base["context"], run)
    final_state = await first_graph.invoke(service._initial_state(run, run.agent_version_id))
    assert final_state["final_output"] == "restart-safe answer"

    original_commit = AsyncSession.commit

    async def fail_terminal_commit(session):
        await session.flush()
        raise RuntimeError("lose terminal commit response")

    monkeypatch.setattr(AsyncSession, "commit", fail_terminal_commit)
    with pytest.raises(RuntimeError, match="lose terminal commit response"):
        await service._complete_run(
            base["context"],
            run.id,
            status=final_state.get("run_status") or "SUCCEEDED",
            final_output=final_state.get("final_output"),
            failure_code=final_state.get("failure_code"),
            model_step_count=first_graph.model_round_count,
            tool_call_count=first_graph.tool_call_count,
            usage_records=first_graph.usage_records,
            pending_finish=first_graph.pending_finish,
        )

    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    async with db_factory() as session:
        persisted = await session.get(AgentRun, run.id)
    assert persisted is not None
    assert (persisted.status, persisted.completed_at) == ("RUNNING", None)

    second_graph = _AgentRunGraph(service, base["context"], run)
    recovered_state = await second_graph.invoke(service._initial_state(run, run.agent_version_id))
    assert recovered_state["final_output"] == "restart-safe answer"
    result = await service._complete_run(
        base["context"],
        run.id,
        status=recovered_state.get("run_status") or "SUCCEEDED",
        final_output=recovered_state.get("final_output"),
        failure_code=recovered_state.get("failure_code"),
        model_step_count=second_graph.model_round_count,
        tool_call_count=second_graph.tool_call_count,
        usage_records=second_graph.usage_records,
        pending_finish=second_graph.pending_finish,
    )
    assert (result.status, result.final_output) == ("SUCCEEDED", "restart-safe answer")
    async with db_factory() as session:
        finish_steps = list(
            await session.scalars(
                select(RunStep).where(
                    RunStep.workspace_id == base["workspace_id"],
                    RunStep.agent_run_id == run.id,
                    RunStep.kind == "FINISH",
                )
            )
        )
    assert len(finish_steps) == 1
    assert finish_steps[0].status == result.status


@pytest.mark.asyncio
async def test_stale_approval_request_preserves_current_interrupt(db_factory) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    async with db_factory() as session:
        from packages.approvals.models import Approval

        stale = Approval(
            workspace_id=base["workspace_id"],
            run_id=waiting.run_id,
            requested_by=base["context"].user_id,
            agent_version_id=base["version"].id,
            tool_revision_id=base["revision"].id,
            tool_identity="create_ticket",
            logical_action_id=f"stale-{uuid4().hex}",
            canonical_arguments={"customer_ref": "cust", "subject": "stale"},
            canonical_args_hash="0" * 64,
            idempotency_key=uuid4().hex,
            decision_status=ApprovalDecisionStatus.APPROVED,
        )
        session.add(stale)
        await session.commit()

    with pytest.raises(AgentHubError) as raised:
        await service.resume(context, run_id=waiting.run_id, approval_id=stale.id)
    assert raised.value.code == "APPROVAL_RESUME_MISMATCH"
    async with db_factory() as session:
        stored = await session.get(AgentRun, waiting.run_id)
    assert stored is not None
    assert (stored.status, stored.waiting_approval_id) == (
        "WAITING_APPROVAL",
        approval.id,
    )


@pytest.mark.asyncio
async def test_checkpoint_missing_interrupt_fails_closed(db_factory) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await ApprovalService(db_factory).decide(
        context, approval.id, decision=ApprovalDecisionStatus.APPROVED
    )
    checkpoint_adapter = service.checkpoint_adapter
    original_checkpointer = checkpoint_adapter.checkpointer

    class Adapter:
        def __getattr__(self, name):
            return getattr(checkpoint_adapter, name)

        def checkpointer(self):
            manager = original_checkpointer()

            class Manager:
                async def __aenter__(self):
                    class Missing:
                        pending_writes = ()

                    return Missing()

                async def __aexit__(self, *args):
                    return await manager.__aexit__(*args)

            return Manager()

    service.checkpoint_adapter = Adapter()
    with pytest.raises(AgentHubError) as raised:
        await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    assert raised.value.code == "APPROVAL_CHECKPOINT_INVALID"
    async with db_factory() as session:
        run = await session.get(AgentRun, waiting.run_id)
    assert run is not None
    assert (run.status, run.failure_code) == (
        "NEEDS_ATTENTION",
        "APPROVAL_CHECKPOINT_INVALID",
    )


@pytest.mark.asyncio
async def test_finish_metadata_uses_effective_attention_outcome(db_factory) -> None:
    base, gateway = await _seed_approval(db_factory)
    context, service, waiting, approval = await _waiting_approval(db_factory, base, gateway)
    await service.approval_service.claim_execution(context, approval.id)
    await service.approval_service.complete_execution(
        context,
        approval.id,
        status=ApprovalExecutionStatus.UNKNOWN_OUTCOME,
        failure_code="ACTION_OUTCOME_UNKNOWN",
        safe_failure_message="Outcome needs reconciliation.",
    )
    result = await service._complete_run(
        context,
        waiting.run_id,
        status="SUCCEEDED",
        final_output="must not be exposed",
        failure_code=None,
        model_step_count=1,
        tool_call_count=1,
        usage_records=[],
        pending_finish={
            "status": "SUCCEEDED",
            "safe_metadata": {"status": "SUCCEEDED", "error_code": None},
        },
    )
    assert (result.status, result.final_output) == ("NEEDS_ATTENTION", None)
    async with db_factory() as session:
        step = await session.scalar(
            select(RunStep).where(
                RunStep.workspace_id == base["workspace_id"],
                RunStep.agent_run_id == waiting.run_id,
                RunStep.kind == "FINISH",
            )
        )
    assert step is not None
    assert step.status == result.status
    assert step.safe_metadata["status"] == result.status
    assert step.safe_metadata["error_code"] == result.failure_code
