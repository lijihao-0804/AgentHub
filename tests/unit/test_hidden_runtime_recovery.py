"""Regression of crash boundaries, source cursors and admitted task identity."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.context_budget import ContextCategory
from packages.agent_runtime.event_store import RunEventRecorder
from packages.agent_runtime.events import AgentEventEmitter, AgentEventType
from packages.agent_runtime.runtime_graph import _retain_admission
from packages.agent_runtime.runtime_service import AgentRunService
from packages.approvals.reconciliation import _apply_reconciliation_transition
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.tools.actions import ActionRuntime
from packages.tools.contracts import ToolEffect
from tests.unit.test_durable_run_stream import build_hub, make_event
from tests.unit.test_runtime_rag_evidence_projection import _definition, _frozen_spec, _graph


@pytest.mark.asyncio
async def test_failed_event_commit_cannot_be_published():
    writes = []

    async def fail_commit(event):
        writes.append(event.sequence)
        raise RuntimeError("commit unavailable")

    emitter = AgentEventEmitter(
        run_id=str(uuid4()), agent_version_id=str(uuid4()), before_publish=fail_commit
    )
    visible = []
    with pytest.raises(RuntimeError, match="commit unavailable"):
        visible.append(await emitter.emit(AgentEventType.RUN_STARTED, {"status": "RUNNING"}))
    assert writes == [1] and visible == []


@pytest.mark.asyncio
async def test_event_database_failure_is_explicit_and_does_not_log_payload(caplog):
    class BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, *_):
            raise RuntimeError("private-output-and-credential-in-database-parameters")

    recorder = RunEventRecorder(lambda: BrokenSession(), workspace_id=uuid4(), run_id=uuid4())
    emitter = AgentEventEmitter(run_id=str(uuid4()), agent_version_id=str(uuid4()))
    event = await emitter.emit(
        AgentEventType.RUN_COMPLETED, {"status": "SUCCEEDED", "output": "private-output"}
    )
    with pytest.raises(AgentHubError) as error:
        await recorder.arecord(event)
    assert error.value.code == "AGENT_EVENT_LOG_UNAVAILABLE"
    assert "private-output" not in caplog.text and "credential" not in caplog.text
    assert recorder._failed


@pytest.mark.parametrize("decision", ["APPROVED", "DENIED", "EXPIRED"])
def test_decided_approval_has_durable_resume_outbox(decision):
    run = SimpleNamespace(status="WAITING_APPROVAL", failure_code=None)
    approval = SimpleNamespace(decision_status=decision, execution_status="NOT_STARTED")
    _apply_reconciliation_transition(run, [], True, approvals=[approval], stale=True)
    assert run.resume_pending is True
    assert run.status == "WAITING_APPROVAL"


def test_stale_claim_cannot_be_reexecuted_and_cancel_converges():
    run = SimpleNamespace(status="WAITING_APPROVAL", failure_code=None)
    claim = SimpleNamespace(decision_status="APPROVED", execution_status="CLAIMED")
    _apply_reconciliation_transition(run, [], True, approvals=[claim], stale=True)
    assert (run.status, claim.execution_status) == ("NEEDS_ATTENTION", "UNKNOWN_OUTCOME")
    queued = SimpleNamespace(status="CANCEL_REQUESTED", failure_code=None)
    _apply_reconciliation_transition(queued, [], False, approvals=[], stale=True)
    assert queued.status == "CANCELLED"


def test_live_owner_is_not_reconciled_as_stale_action():
    run = SimpleNamespace(
        status="CANCEL_REQUESTED",
        failure_code=None,
        execution_owner="owner",
        execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    claim = SimpleNamespace(decision_status="APPROVED", execution_status="CLAIMED")
    _apply_reconciliation_transition(run, [], True, approvals=[claim], stale=True)
    assert run.status == "CANCEL_REQUESTED" and claim.execution_status == "CLAIMED"


class Session:
    def __init__(self, run):
        self.run = run

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def scalar(self, statement):
        return self.run

    async def commit(self):
        pass

    async def refresh(self, run):
        pass


@pytest.mark.asyncio
async def test_unknown_action_wins_over_concurrent_cancel():
    run = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        agent_version_id=uuid4(),
        status="CANCELLED",
        event_log_complete=True,
        final_output=None,
        failure_code=None,
    )
    service = AgentRunService.__new__(AgentRunService)
    service.session_factory = lambda: Session(run)

    async def no_memory(*args):
        pass

    service._enqueue_memory_extraction = no_memory
    result = await service._complete_run(
        SimpleNamespace(workspace_id=str(run.workspace_id)),
        run.id,
        status="NEEDS_ATTENTION",
        final_output=None,
        failure_code="ACTION_RECONCILIATION_REQUIRED",
        model_step_count=1,
        tool_call_count=1,
        usage_records=[],
    )
    assert result.status == "NEEDS_ATTENTION"
    assert result.failure_code == "ACTION_RECONCILIATION_REQUIRED"
    assert run.event_log_complete is False


@pytest.mark.asyncio
async def test_post_commit_timeout_is_never_determinate_failure():
    tickets = []

    class TicketSession(Session):
        reads = 0

        async def scalar(self, statement):
            self.reads += 1
            return None if self.reads == 1 else SimpleNamespace(id=uuid4())

        def add(self, ticket):
            self.ticket = ticket

        async def commit(self):
            tickets.append(self.ticket)

        async def refresh(self, ticket):
            await asyncio.Event().wait()

    runtime = ActionRuntime(session_factory=lambda: TicketSession(None))
    definition = replace(
        _definition(identity="create_ticket"), effect=ToolEffect.WRITE, timeout_seconds=0.01
    )
    result = await runtime.execute(
        SimpleNamespace(workspace_id=str(uuid4())),
        definition,
        {"customer_ref": "customer", "subject": "issue"},
        idempotency_key="stable-attempt",
    )
    assert len(tickets) == 1 and result.status.value == "UNKNOWN_OUTCOME"


@pytest.mark.asyncio
async def test_abort_before_checkpoint_restore_preserves_already_spent_usage():
    run = SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        agent_version_id=uuid4(),
        status="CANCEL_REQUESTED",
        final_output=None,
        failure_code=None,
        model_step_count=2,
        tool_call_count=3,
        total_input_tokens=100,
        total_output_tokens=30,
        total_tokens=130,
        total_cached_tokens=0,
        total_cost_amount="0.01",
        cost_currency="USD",
    )
    service = AgentRunService.__new__(AgentRunService)
    service.session_factory = lambda: Session(run)

    async def no_memory(*args):
        pass

    service._enqueue_memory_extraction = no_memory
    result = await service._complete_run(
        SimpleNamespace(workspace_id=str(run.workspace_id)),
        run.id,
        status="FAILED",
        final_output=None,
        failure_code="STREAM_ABORTED",
        model_step_count=0,
        tool_call_count=0,
        usage_records=[],
    )
    assert result.status == "CANCELLED"
    assert (result.model_step_count, result.tool_call_count) == (2, 3)
    assert result.total_tokens == 130 and result.total_cost_amount == "0.01"


@pytest.mark.asyncio
async def test_attach_does_not_jump_over_durable_replay_live_gap():
    persisted = [make_event(10)]

    class Reader:
        async def read_after(self, **kwargs):
            return [e for e in persisted if e.sequence > kwargs["after_sequence"]]

    class Hub:
        closed = True

        async def subscribe(self):
            persisted.extend([make_event(11), make_event(12)])
            yield persisted[-1]

    service = AgentRunService.__new__(AgentRunService)
    service._require_permission = lambda *args: None

    async def get(*args):
        return SimpleNamespace(agent_version_id=uuid4())

    service.get_run = get
    service.event_reader = Reader()
    service.stream_registry = SimpleNamespace(get=lambda _: Hub())
    got = [
        e.sequence
        async for e in service.attach_stream(
            SimpleNamespace(workspace_id=str(uuid4())), run_id=uuid4()
        )
    ]
    assert got == [10, 11, 12]


@pytest.mark.asyncio
async def test_contiguous_live_tokens_do_not_trigger_one_database_query_each():
    class Reader:
        calls = 0

        async def read_after(self, **kwargs):
            self.calls += 1
            return [make_event(10)] if kwargs["after_sequence"] < 10 else []

    class Hub:
        closed = True

        async def subscribe(self):
            for sequence in range(11, 61):
                yield make_event(sequence, AgentEventType.MESSAGE_DELTA)

    service = AgentRunService.__new__(AgentRunService)
    service._require_permission = lambda *_: None

    async def get(*_):
        return SimpleNamespace(agent_version_id=uuid4())

    service.get_run = get
    service.event_reader = Reader()
    service.stream_registry = SimpleNamespace(get=lambda _: Hub())
    events = [
        event
        async for event in service.attach_stream(
            SimpleNamespace(workspace_id=str(uuid4())), run_id=uuid4()
        )
    ]
    # Initial page + exhausted page + final tail; no reads per token.
    assert len(events) == 51 and service.event_reader.calls == 3


@pytest.mark.asyncio
async def test_overflowed_subscriber_terminates_instead_of_waiting_forever():
    hub, source, aborted = build_hub(grace_seconds=None)

    async def consume():
        return [event async for event in hub.subscribe()]

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    for sequence in range(1, 520):
        hub._broadcast(make_event(sequence, AgentEventType.MESSAGE_DELTA))
    received = await asyncio.wait_for(consumer, 1)
    assert received and not aborted
    await hub.aclose()


def test_second_admission_keeps_current_task_after_history_eviction():
    graph = _graph()
    state = {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            *[
                m
                for i in range(4)
                for m in (
                    ModelMessage(role="user", content=f"old {i}" + "x" * 120),
                    ModelMessage(role="assistant", content=f"reply {i}" + "y" * 120),
                )
            ],
            ModelMessage(role="user", content="CURRENT TASK" * 8),
        ],
        "spec": _frozen_spec(200),
        "runtime": {
            "context_budget": {
                "reserved_output_tokens": 1,
                "max_retrieval_tokens": 100,
                "max_tool_result_tokens": 100,
            }
        },
        "tool_definitions": {},
        "history_message_count": 8,
        "memory_message_count": 0,
    }
    first = graph._admit_context(state)
    assert first.usage.dropped_exchange_count > 0
    _retain_admission(state, first)
    assert state["history_message_count"] < 8
    state["messages"].extend(
        [
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(name="lookup", arguments={}, provider_tool_call_id="call"),
                ),
            ),
            ModelMessage(role="tool", name="lookup", tool_call_id="call", content="result"),
        ]
    )
    second = graph._admit_context(state)
    assert any(m.content == "CURRENT TASK" * 8 for m in second.messages)
    assert ContextCategory.CURRENT_USER_TASK in second.message_categories
