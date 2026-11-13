"""Thread streaming follows the non-stream worker dispatch contract."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from apps.api.routes import threads
from packages.core.errors.exceptions import AgentHubError
from packages.threads.service import PreparedTurnRun

_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000010")
_THREAD_ID = UUID("00000000-0000-0000-0000-000000000020")
_TURN_ID = UUID("00000000-0000-0000-0000-000000000030")
_VERSION_ID = UUID("00000000-0000-0000-0000-000000000040")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000050")
_REQUEST_ID = "thread-stream-request"


async def _empty_stream():
    if False:
        yield None


class _Queue:
    def __init__(self, *, fails: bool = False) -> None:
        self.calls: list[dict] = []
        self.fails = fails

    async def enqueue(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.fails:
            raise RuntimeError("broker unavailable")


class _RunService:
    def __init__(self) -> None:
        self.attached: list[tuple[object, UUID]] = []
        self.streamed: list[dict] = []

    def attach_stream(self, context, *, run_id):
        self.attached.append((context, run_id))
        return _empty_stream()

    def stream(self, context, **kwargs):
        self.streamed.append({"context": context, **kwargs})
        return _empty_stream()


class _ThreadService:
    def __init__(self, prepared_run: PreparedTurnRun) -> None:
        self.prepared_run = prepared_run
        self.ensure_calls: list[dict] = []

    async def resolve_agent_version(self, _context, thread_id):
        return SimpleNamespace(id=thread_id), _VERSION_ID

    async def open_turn(self, _context, thread_id, **kwargs):
        assert thread_id == _THREAD_ID
        return SimpleNamespace(id=_TURN_ID, user_input=kwargs["user_input"])

    async def ensure_stream_run(self, _context, **kwargs):
        self.ensure_calls.append(kwargs)
        return self.prepared_run


def _prepared(*, dispatchable: bool) -> PreparedTurnRun:
    run = (
        SimpleNamespace(
            id=_RUN_ID,
            status="RUNNING",
            execution_owner=None,
        )
        if dispatchable
        else None
    )
    return PreparedTurnRun(
        turn=SimpleNamespace(id=_TURN_ID),
        run_id=_RUN_ID,
        run=run,
        reused=not dispatchable,
    )


def _request(*, worker_enabled: bool):
    settings = SimpleNamespace(
        run_execution_in_worker=worker_enabled,
        sse_heartbeat_seconds=15,
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


def _context():
    return SimpleNamespace(
        workspace_id=str(_WORKSPACE_ID),
        request_id=_REQUEST_ID,
        permissions=frozenset({"agent_run"}),
    )


async def _stream_turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_enabled: bool,
    prepared_run: PreparedTurnRun,
    queue: _Queue,
    run_service: _RunService | None = None,
):
    thread_service = _ThreadService(prepared_run)
    run_service = run_service or _RunService()
    monkeypatch.setattr(threads, "_service", lambda _request: thread_service)
    monkeypatch.setattr(
        threads,
        "get_production_agent_run_service",
        lambda _request: run_service,
    )
    response = await threads.stream_turn(
        workspace_id=_WORKSPACE_ID,
        thread_id=_THREAD_ID,
        payload=SimpleNamespace(input_text="hello", client_token="client-turn"),
        request=_request(worker_enabled=worker_enabled),
        context=_context(),
        queue=queue,
    )
    return response, thread_service, run_service


@pytest.mark.asyncio
async def test_stream_turn_dispatches_one_worker_message_and_then_attaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    prepared = _prepared(dispatchable=True)

    response, _, run_service = await _stream_turn(
        monkeypatch,
        worker_enabled=True,
        prepared_run=prepared,
        queue=queue,
    )

    assert queue.calls == [
        {
            "workspace_id": _WORKSPACE_ID,
            "run_id": _RUN_ID,
            "request_id": _REQUEST_ID,
        }
    ]
    assert run_service.attached == [(_context(), _RUN_ID)]
    assert run_service.streamed == []
    assert response.headers["X-AgentHub-Run-Id"] == str(_RUN_ID)
    assert response.headers["X-AgentHub-Turn-Id"] == str(_TURN_ID)


@pytest.mark.asyncio
async def test_stream_turn_never_enqueues_an_attached_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()

    _, _, run_service = await _stream_turn(
        monkeypatch,
        worker_enabled=True,
        prepared_run=_prepared(dispatchable=False),
        queue=queue,
    )

    assert queue.calls == []
    assert run_service.attached == [(_context(), _RUN_ID)]
    assert run_service.streamed == []


@pytest.mark.asyncio
async def test_stream_turn_runs_locally_when_worker_gate_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue()
    prepared = _prepared(dispatchable=True)

    _, _, run_service = await _stream_turn(
        monkeypatch,
        worker_enabled=False,
        prepared_run=prepared,
        queue=queue,
    )

    assert queue.calls == []
    assert run_service.attached == []
    assert run_service.streamed == [
        {
            "context": _context(),
            "agent_version_id": _VERSION_ID,
            "input_text": "hello",
            "prepared_run": prepared.run,
            "graceful_disconnect": True,
        }
    ]


@pytest.mark.asyncio
async def test_stream_turn_queue_failure_keeps_an_unowned_running_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _Queue(fails=True)
    prepared = _prepared(dispatchable=True)
    run_service = _RunService()

    with pytest.raises(AgentHubError) as raised:
        await _stream_turn(
            monkeypatch,
            worker_enabled=True,
            prepared_run=prepared,
            queue=queue,
            run_service=run_service,
        )

    assert raised.value.code == "RUN_DISPATCH_UNAVAILABLE"
    assert raised.value.status_code == 503
    assert prepared.run is not None
    assert prepared.run.status == "RUNNING"
    assert prepared.run.execution_owner is None
    assert len(queue.calls) == 1
    assert queue.calls[0]["request_id"] == _REQUEST_ID
    assert run_service.attached == []
    assert run_service.streamed == []
