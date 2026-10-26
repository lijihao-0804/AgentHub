"""PostgreSQL concurrency and crash windows from the hidden-defects audit."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.event_store import RunEventReader, RunEventRecorder
from packages.agent_runtime.events import AgentEventEmitter, AgentEventType
from packages.agent_runtime.execution_lease import run_execution_lease
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals import (
    ApprovalDecisionStatus,
    ApprovalReconciliationService,
    ApprovalService,
)
from packages.approvals.models import Approval
from packages.control_plane.models import OrganizationMembership, User, WorkspaceMembership
from packages.control_plane.services import TenantService
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import (
    TEST_DATABASE_URL,
)
from tests.integration.test_m5a_approval_runtime import db_factory as db_factory
from tests.integration.test_m5a_approval_runtime import migrated_database as migrated_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="PostgreSQL required"),
]

TOOL_SPEC = {
    "kind": "builtin",
    "identity": "create_ticket",
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


async def seed_run(factory, *, approval=False, status="RUNNING"):
    async with factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=TOOL_SPEC if approval else None)
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            input_text="recover this exact run",
            created_by=base["user"].id,
            status=status,
        )
        session.add(run)
        await session.commit()
    return base, run


@pytest.mark.asyncio
async def test_two_workers_have_one_durable_execution_owner(db_factory):
    base, run = await seed_run(db_factory)
    entered, finish = asyncio.Event(), asyncio.Event()
    executions = []

    async def first_worker():
        async with run_execution_lease(
            db_factory, run.workspace_id, run.id, allowed_statuses={"RUNNING"}
        ) as token:
            assert token
            executions.append(token)
            entered.set()
            await finish.wait()

    first = asyncio.create_task(first_worker())
    await entered.wait()
    try:
        async with run_execution_lease(
            db_factory, run.workspace_id, run.id, allowed_statuses={"RUNNING"}
        ) as token:
            assert token is None
        async with db_factory() as session:
            stored = await session.get(AgentRun, run.id)
            assert stored.execution_owner == executions[0]
    finally:
        finish.set()
        await first
    async with db_factory() as session:
        assert (await session.get(AgentRun, run.id)).execution_owner is None
    assert len(executions) == 1


@pytest.mark.asyncio
async def test_duplicate_service_execution_calls_model_once(db_factory):
    base, run = await seed_run(db_factory)
    entered, release = asyncio.Event(), asyncio.Event()

    class Gateway:
        calls = 0

        async def generate_resolved(self, *_):
            self.calls += 1
            entered.set()
            await release.wait()
            return ModelResponse(content="one answer", provider="fake", model="fake")

    gateway = Gateway()
    service = AgentRunService(db_factory, model_gateway_factory=lambda session: gateway)
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "workspace_read"})}
    )
    first = asyncio.create_task(service.execute_prepared_run(context, run_id=run.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = await service.execute_prepared_run(context, run_id=run.id)
        assert duplicate.status == "RUNNING" and gateway.calls == 1
    finally:
        release.set()
    assert (await first).status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_duplicate_resume_does_not_execute_action_or_model_again(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=TOOL_SPEC)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="cust", name="Single action")
        )
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    class Gateway:
        calls = 0

        async def generate_resolved(self, *_):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    content="",
                    provider="fake",
                    model="fake",
                    tool_calls=(
                        ModelToolCall(
                            name="create_ticket",
                            arguments={"customer_ref": "cust", "subject": "once"},
                            provider_tool_call_id="provider-id",
                        ),
                    ),
                )
            entered.set()
            await release.wait()
            return ModelResponse(content="done", provider="fake", model="fake")

    gateway = Gateway()
    approvals = ApprovalService(db_factory)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=approvals,
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )
    context = base["context"].model_copy(
        update={
            "permissions": frozenset({"agent_run", "tool_run", "approve_action", "workspace_read"})
        }
    )
    waiting = await service.run(context, agent_version_id=base["version"].id, input_text="once")
    approval = (await approvals.list(context))[0]
    await approvals.decide(context, approval.id, decision=ApprovalDecisionStatus.APPROVED)
    first = asyncio.create_task(
        service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = await service.resume(context, run_id=waiting.run_id, approval_id=approval.id)
        assert duplicate.status == "RUNNING" and gateway.calls == 2
    finally:
        release.set()
    assert (await first).status == "SUCCEEDED"
    async with db_factory() as session:
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
        saved = await session.get(Approval, approval.id)
    assert len(tickets) == 1 and saved.execution_attempt_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["api", "worker"])
@pytest.mark.parametrize("access", ["viewer", "removed"])
async def test_approval_resume_rechecks_original_actor_after_role_downgrade(
    db_factory,
    monkeypatch,
    delivery,
    access,
):
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=TOOL_SPEC)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="cust", name="Retained")
        )
        await session.commit()

    class Gateway:
        calls = 0

        async def generate_resolved(self, *_):
            self.calls += 1
            return ModelResponse(
                content="",
                provider="fake",
                model="fake",
                tool_calls=(
                    ModelToolCall(
                        name="create_ticket",
                        arguments={"customer_ref": "cust", "subject": "must not execute"},
                        provider_tool_call_id="request",
                    ),
                ),
            )

    gateway = Gateway()
    approvals = ApprovalService(db_factory)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=approvals,
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )
    waiting = await service.run(
        base["context"], agent_version_id=base["version"].id, input_text="approval"
    )
    async with db_factory() as session:
        email = f"new-approver-{uuid4().hex}@example.test"
        admin = User(email=email, normalized_email=email, password_hash="not-used")
        session.add(admin)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=base["organization_id"], user_id=admin.id, role="OWNER"
            )
        )
        actor = await session.get(
            OrganizationMembership, (base["organization_id"], base["user"].id)
        )
        actor.role = "MEMBER"
        if access == "viewer":
            session.add(
                WorkspaceMembership(
                    workspace_id=base["workspace_id"], user_id=base["user"].id, role="VIEWER"
                )
            )
        await session.commit()
        admin_context = (
            await TenantService().get_workspace_access(
                session,
                principal=PrincipalContext(
                    request_id="revoked-resume", trace_id="revoked-resume", user_id=str(admin.id)
                ),
                workspace_id=base["workspace_id"],
            )
        ).context
    approval = (await approvals.list(admin_context))[0]
    await approvals.decide(admin_context, approval.id, decision=ApprovalDecisionStatus.APPROVED)
    if delivery == "api":
        denied = await service.resume(admin_context, run_id=waiting.run_id, approval_id=approval.id)
        assert denied.status == "NEEDS_ATTENTION"
    else:
        from apps.worker.tasks import agent_runs as worker

        def no_service(*_):
            raise AssertionError("A revoked actor must not initialize a model runtime")

        monkeypatch.setattr(worker, "_build_service", no_service)
        await worker._resume_agent_run(
            Settings(_env_file=None, database_url=TEST_DATABASE_URL), waiting.run_id, approval.id
        )
    async with db_factory() as session:
        retained = await session.get(AgentRun, waiting.run_id)
        pending = await session.get(Approval, approval.id)
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert (
        retained.status == "NEEDS_ATTENTION" and retained.failure_code == "RUN_ACTOR_ACCESS_REVOKED"
    )
    assert not retained.resume_pending and not retained.event_log_complete
    assert pending.execution_status == "NOT_STARTED"
    assert gateway.calls == 1 and tickets == []


@pytest.mark.asyncio
async def test_dead_owner_is_fenced_without_replay(db_factory):
    _, run = await seed_run(db_factory)
    async with db_factory() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(
                execution_owner="dead-worker",
                execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()
    async with run_execution_lease(
        db_factory, run.workspace_id, run.id, allowed_statuses={"RUNNING"}
    ) as token:
        assert token is None
    async with db_factory() as session:
        stored = await session.get(AgentRun, run.id)
        assert (stored.status, stored.failure_code) == (
            "NEEDS_ATTENTION",
            "RUN_EXECUTION_LEASE_EXPIRED",
        )


@pytest.mark.asyncio
async def test_interrupted_owner_does_not_leave_runnable_orphan(db_factory):
    _, run = await seed_run(db_factory)
    with pytest.raises(RuntimeError, match="crash boundary"):
        async with run_execution_lease(
            db_factory, run.workspace_id, run.id, allowed_statuses={"RUNNING"}
        ):
            raise RuntimeError("crash boundary")
    async with run_execution_lease(
        db_factory, run.workspace_id, run.id, allowed_statuses={"RUNNING"}
    ) as token:
        assert token is None
    async with db_factory() as session:
        stored = await session.get(AgentRun, run.id)
        assert stored.status == "NEEDS_ATTENTION" and stored.execution_owner is None


@pytest.mark.asyncio
async def test_decision_commit_recovers_without_http_resume_and_fences_claim(db_factory):
    base, run = await seed_run(db_factory, approval=True, status="WAITING_APPROVAL")
    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_run", "approve_action"})}
    )
    service = ApprovalService(db_factory)
    approval = await service.create_or_get(
        context,
        run_id=run.id,
        agent_version_id=base["version"].id,
        tool_revision_id=base["revision"].id,
        tool_identity="create_ticket",
        arguments={"customer_ref": "cust", "subject": "recovery"},
        input_schema=TOOL_SPEC["input_schema"],
        proposal_ordinal=0,
    )
    await service.decide(context, approval.id, decision=ApprovalDecisionStatus.APPROVED)
    async with db_factory() as session:
        assert (await session.get(AgentRun, run.id)).resume_pending is True
        stored = await session.get(AgentRun, run.id)
        stored.started_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
    delivered = []

    class Probe:
        async def has_checkpoint(self, *_):
            return True

    async def dispatch(stored, decided):
        delivered.append((stored.id, decided.id))

    reconciler = ApprovalReconciliationService(
        db_factory, checkpoint_probe=Probe(), resume_dispatch=dispatch
    )
    result = await reconciler.reconcile_candidates(batch_size=10000, stale_after_seconds=0)
    assert result["errors"] == 0 and (run.id, approval.id) in delivered
    await service.claim_execution(context, approval.id)
    recovered = await reconciler.reconcile_run(context, run.id, checkpoint_exists=True)
    assert recovered.status == "NEEDS_ATTENTION"
    async with db_factory() as session:
        assert (await session.get(Approval, approval.id)).execution_status == "UNKNOWN_OUTCOME"
    assert await service.claim_execution(context, approval.id) is None


@pytest.mark.asyncio
async def test_event_commit_precedes_publish_and_resume_sequence(db_factory):
    _, run = await seed_run(db_factory)
    recorder = RunEventRecorder(db_factory, workspace_id=run.workspace_id, run_id=run.id)
    reader = RunEventReader(db_factory)
    emitter = AgentEventEmitter(
        run_id=str(run.id),
        agent_version_id=str(run.agent_version_id),
        before_publish=recorder.arecord,
    )
    event = await emitter.emit(AgentEventType.RUN_STARTED, {"status": "RUNNING"})
    await recorder.arecord(event)  # Lost commit acknowledgements may retry the same frame.
    assert (
        await reader.latest_sequence(workspace_id=run.workspace_id, run_id=run.id) == event.sequence
    )
    resumed = AgentEventEmitter(
        run_id=str(run.id),
        agent_version_id=str(run.agent_version_id),
        initial_sequence=await reader.latest_sequence(workspace_id=run.workspace_id, run_id=run.id),
        before_publish=recorder.arecord,
    )
    end = await resumed.emit(
        AgentEventType.RUN_COMPLETED, {"status": "SUCCEEDED", "output": "done"}
    )
    assert end.sequence > event.sequence
    await recorder.aclose()
    async with db_factory() as session:
        assert not (await session.get(AgentRun, run.id)).event_log_complete

    async with db_factory() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == run.id).values(status="SUCCEEDED")
        )
        await session.commit()
    await recorder.aclose()
    async with db_factory() as session:
        assert (await session.get(AgentRun, run.id)).event_log_complete


@pytest.mark.asyncio
async def test_conflicting_event_sequence_is_not_silently_dropped(db_factory):
    _, run = await seed_run(db_factory)
    recorder = RunEventRecorder(db_factory, workspace_id=run.workspace_id, run_id=run.id)
    emitter = AgentEventEmitter(run_id=str(run.id), agent_version_id=str(run.agent_version_id))
    await recorder.arecord(await emitter.emit(AgentEventType.RUN_STARTED, {"status": "RUNNING"}))
    other = AgentEventEmitter(run_id=str(run.id), agent_version_id=str(run.agent_version_id))
    with pytest.raises(ValueError, match="Conflicting durable"):
        await recorder.arecord(await other.emit(AgentEventType.RUN_STARTED, {"status": "RUNNING"}))
    await recorder.aclose()
    async with db_factory() as session:
        assert not (await session.get(AgentRun, run.id)).event_log_complete
        await session.execute(
            update(AgentRun).where(AgentRun.id == run.id).values(status="SUCCEEDED")
        )
        await session.commit()
    fresh = RunEventRecorder(db_factory, workspace_id=run.workspace_id, run_id=run.id)
    await fresh.aclose()
    async with db_factory() as session:
        assert not (await session.get(AgentRun, run.id)).event_log_complete


@pytest.mark.asyncio
async def test_owner_loss_without_terminal_frame_cannot_claim_complete_log(db_factory):
    _, run = await seed_run(db_factory)
    recorder = RunEventRecorder(db_factory, workspace_id=run.workspace_id, run_id=run.id)
    emitter = AgentEventEmitter(run_id=str(run.id), agent_version_id=str(run.agent_version_id))
    await recorder.arecord(await emitter.emit(AgentEventType.RUN_STARTED, {"status": "RUNNING"}))
    async with db_factory() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == run.id).values(status="NEEDS_ATTENTION")
        )
        await session.commit()
    await recorder.aclose()
    async with db_factory() as session:
        assert not (await session.get(AgentRun, run.id)).event_log_complete


@pytest.mark.asyncio
async def test_probe_failure_does_not_starve_later_batch(db_factory):
    _, first = await seed_run(db_factory)
    _, second = await seed_run(db_factory)
    async with db_factory() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id.in_([first.id, second.id]))
            .values(started_at=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()
    seen = []

    class Probe:
        async def has_checkpoint(self, workspace_id, run_id):
            seen.append(run_id)
            raise RuntimeError("probe unavailable")

    reconciler = ApprovalReconciliationService(db_factory, checkpoint_probe=Probe())
    # Other fixtures may leave active runs in the shared disposable DB; each
    # failed page must still advance so every candidate is eventually visited.
    async with db_factory() as session:
        count = len(
            list(
                await session.scalars(
                    select(AgentRun.id).where(
                        AgentRun.status.in_(
                            {"RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED", "NEEDS_ATTENTION"}
                        )
                    )
                )
            )
        )
    for _ in range(count + 1):
        await reconciler.reconcile_candidates(batch_size=1, stale_after_seconds=0)
    assert first.id in seen and second.id in seen


@pytest.mark.asyncio
async def test_second_approval_uses_active_interrupt_and_old_resume_cannot_poison_run(db_factory):
    async with db_factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=TOOL_SPEC)
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="cust", name="Recovery")
        )
        await session.commit()

    class Gateway:
        def __init__(self):
            self.calls = 0

        async def generate_resolved(self, context, plan, request):
            self.calls += 1
            if self.calls <= 2:
                return ModelResponse(
                    content="",
                    provider="fake",
                    model="fake",
                    tool_calls=(
                        ModelToolCall(
                            name="create_ticket",
                            arguments={"customer_ref": "cust", "subject": f"request {self.calls}"},
                            provider_tool_call_id=f"call-{self.calls}",
                        ),
                    ),
                )
            return ModelResponse(content="done", provider="fake", model="fake")

    gateway = Gateway()
    approvals = ApprovalService(db_factory)
    service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        approval_service=approvals,
        action_runtime=ActionRuntime(session_factory=db_factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )
    context = base["context"].model_copy(
        update={
            "permissions": frozenset({"agent_run", "tool_run", "approve_action", "workspace_read"})
        }
    )
    first = await service.run(
        context, agent_version_id=base["version"].id, input_text="two approvals"
    )
    old = (await approvals.list(context))[0]
    await approvals.decide(context, old.id, decision=ApprovalDecisionStatus.DENIED)
    second = await service.resume(context, run_id=first.run_id, approval_id=old.id)
    assert second.status == "WAITING_APPROVAL"
    pending = next(a for a in await approvals.list(context) if a.decision_status == "PENDING")
    async with db_factory() as session:
        run = await session.get(AgentRun, first.run_id)
        assert run.waiting_approval_id == pending.id and not run.resume_pending
    with pytest.raises(AgentHubError, match="active durable interrupt"):
        await service.resume(context, run_id=first.run_id, approval_id=old.id)
    async with db_factory() as session:
        assert (await session.get(AgentRun, first.run_id)).status == "WAITING_APPROVAL"
    await approvals.decide(context, pending.id, decision=ApprovalDecisionStatus.APPROVED)
    delivered = []

    class Probe:
        async def has_checkpoint(self, *_):
            return True

    async def dispatch(run, approval):
        if run.id == first.run_id:
            delivered.append(approval.id)

    async with db_factory() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == first.run_id)
            .values(started_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()
    await ApprovalReconciliationService(
        db_factory, checkpoint_probe=Probe(), resume_dispatch=dispatch
    ).reconcile_candidates(batch_size=10000, stale_after_seconds=0)
    assert delivered == [pending.id]
    done = await service.resume(context, run_id=first.run_id, approval_id=pending.id)
    assert done.status == "SUCCEEDED"
    async with db_factory() as session:
        tickets = list(
            await session.scalars(select(Ticket).where(Ticket.workspace_id == base["workspace_id"]))
        )
    assert len(tickets) == 1
