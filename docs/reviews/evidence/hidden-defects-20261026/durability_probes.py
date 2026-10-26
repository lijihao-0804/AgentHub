"""Deterministic state/sequence probes; no PostgreSQL or external calls."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals.reconciliation import _apply_reconciliation_transition
from packages.tools.actions import ActionRuntime
from packages.tools.contracts import ToolEffect
from tests.unit.test_durable_run_stream import make_event
from tests.unit.test_runtime_rag_evidence_projection import _definition


@pytest.mark.parametrize(
    "decision,execution",
    [
        ("APPROVED", "NOT_STARTED"),
        ("DENIED", "NOT_STARTED"),
        ("EXPIRED", "NOT_STARTED"),
        ("APPROVED", "CLAIMED"),
    ],
)
def test_reconciler_does_not_converge_decided_or_claimed_approval(decision, execution):
    run = SimpleNamespace(status="WAITING_APPROVAL", failure_code=None)
    approval = SimpleNamespace(decision_status=decision, execution_status=execution)
    _apply_reconciliation_transition(run, [], True, approvals=[approval], stale=True)
    assert run.status == "WAITING_APPROVAL"
    assert approval.execution_status == execution


def test_pre_execution_cancel_is_not_converged_by_reconciler():
    run = SimpleNamespace(status="CANCEL_REQUESTED", failure_code=None)
    _apply_reconciliation_transition(run, [], True, approvals=[], stale=True)
    assert run.status == "CANCEL_REQUESTED"


class Session:
    def __init__(self, run):
        self.run = run

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def scalar(self, _statement):
        return self.run

    async def commit(self):
        pass

    async def refresh(self, _run):
        pass


@pytest.mark.asyncio
async def test_cancelled_run_overrides_unknown_action_result():
    service = AgentRunService.__new__(AgentRunService)
    run = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        agent_version_id=uuid4(),
        status="CANCELLED",
        final_output=None,
        failure_code=None,
    )
    service.session_factory = lambda: Session(run)

    async def no_memory(*_args):
        pass

    service._enqueue_memory_extraction = no_memory
    context = SimpleNamespace(workspace_id=str(run.workspace_id))
    result = await service._complete_run(
        context,
        run.id,
        status="NEEDS_ATTENTION",
        final_output=None,
        failure_code="ACTION_RECONCILIATION_REQUIRED",
        model_step_count=1,
        tool_call_count=1,
        usage_records=[],
    )
    assert result.status == "CANCELLED"
    assert result.failure_code is None


@pytest.mark.asyncio
async def test_replay_live_gap_skips_durably_stored_event():
    service = AgentRunService.__new__(AgentRunService)
    run = SimpleNamespace(id=uuid4(), agent_version_id=uuid4())
    context = SimpleNamespace(workspace_id=str(uuid4()), permissions={"agent_run"})
    events = [make_event(10)]

    class Reader:
        calls = 0

        async def read_after(self, *, after_sequence, **_kwargs):
            self.calls += 1
            result = [event for event in events if event.sequence > after_sequence]
            if self.calls == 2:
                # Event 11 commits after replay observed no newer rows.
                events.append(make_event(11))
            return result

    class Hub:
        async def subscribe(self):
            # The live subscription starts after event 11 was broadcast.
            events.append(make_event(12))
            yield events[-1]

    async def get_run(*_args):
        return run

    service.get_run = get_run
    service.event_reader = Reader()
    service.stream_registry = SimpleNamespace(get=lambda _id: Hub())
    seen = [event.sequence async for event in service.attach_stream(context, run_id=run.id)]
    assert [event.sequence for event in events] == [10, 11, 12]
    assert seen == [10, 12]


@pytest.mark.asyncio
async def test_worker_drops_cancel_requested_before_execution(monkeypatch):
    from apps.worker.tasks import agent_runs

    run = SimpleNamespace(status="CANCEL_REQUESTED")

    class Engine:
        async def dispose(self):
            pass

    monkeypatch.setattr(
        agent_runs, "create_database", lambda _url: (Engine(), lambda: Session(run))
    )

    def must_not_build(*_args):
        pytest.fail("worker unexpectedly started execution")

    monkeypatch.setattr(agent_runs, "_build_service", must_not_build)
    await agent_runs._execute_agent_run(
        SimpleNamespace(database_url="unused"),
        workspace_id=uuid4(),
        run_id=uuid4(),
        request_id="audit",
    )
    assert run.status == "CANCEL_REQUESTED"


@pytest.mark.asyncio
async def test_builtin_commit_then_timeout_is_reported_as_failed():
    committed = []

    class TicketSession(Session):
        reads = 0

        async def scalar(self, _statement):
            self.reads += 1
            return None if self.reads == 1 else SimpleNamespace(id=uuid4())

        def add(self, ticket):
            self.ticket = ticket

        async def commit(self):
            committed.append(self.ticket)

        async def refresh(self, _ticket):
            await asyncio.Event().wait()

    runtime = ActionRuntime(session_factory=lambda: TicketSession(None))
    definition = replace(
        _definition(identity="create_ticket"), effect=ToolEffect.WRITE, timeout_seconds=0.01
    )
    result = await runtime.execute(
        SimpleNamespace(workspace_id=str(uuid4())),
        definition,
        {"customer_ref": "customer", "subject": "audit"},
        idempotency_key="audit-only",
    )
    assert len(committed) == 1
    assert result.status.value == "FAILED"
    assert result.failure_code == "ACTION_TIMEOUT"
