"""Thread stream routing follows the service's dispatch-safe Run decision."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from apps.api.routes.threads import _stream_mode
from packages.threads.service import PreparedTurnRun, ThreadService

_TURN_ID = UUID("00000000-0000-0000-0000-000000000102")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000105")


def _run(*, status: str, owner: str | None):
    return SimpleNamespace(
        status=status,
        execution_owner=owner,
    )


def _prepared(run) -> PreparedTurnRun:
    return PreparedTurnRun(
        turn=SimpleNamespace(id=_TURN_ID),
        run_id=_RUN_ID,
        run=run,
        reused=run is None,
    )


def test_run_without_a_dispatch_decision_is_followed() -> None:
    assert _stream_mode(_prepared(None)) == "attach"


def test_dispatch_safe_run_is_started() -> None:
    assert _stream_mode(_prepared(_run(status="RUNNING", owner=None))) == "execute"


@pytest.mark.parametrize(
    ("status", "owner", "dispatchable"),
    [
        ("RUNNING", None, True),
        ("RUNNING", "live-owner", False),
        ("RUNNING", "expired-owner", False),
        ("CANCEL_REQUESTED", None, False),
        ("SUCCEEDED", None, False),
        ("FAILED", None, False),
        ("CANCELLED", None, False),
        ("NEEDS_ATTENTION", None, False),
        ("WAITING_APPROVAL", None, False),
    ],
)
def test_only_unowned_running_run_is_safe_to_redispatch(
    status: str, owner: str | None, dispatchable: bool
) -> None:
    assert ThreadService._is_unstarted_run(_run(status=status, owner=owner)) is dispatchable


@pytest.mark.asyncio
async def test_stream_turn_enqueues_to_worker_when_enabled():
    from unittest.mock import AsyncMock, MagicMock

    from apps.api.routes.threads import stream_turn

    enqueued = []
    class FakeQueue:
        async def enqueue(self, workspace_id, run_id, request_id):
            enqueued.append((workspace_id, run_id, request_id))

    class FakeRunService:
        def attach_stream(self, context, run_id):
            async def gen():
                if False:
                    yield None
            return gen()

    thread_service = MagicMock()
    ver_id = UUID("00000000-0000-0000-0000-000000000001")
    thread_service.resolve_agent_version = AsyncMock(return_value=(MagicMock(), ver_id))
    turn_mock = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000002"), user_input="hello"
    )
    thread_service.open_turn = AsyncMock(return_value=turn_mock)
    run_id = UUID("00000000-0000-0000-0000-000000000003")
    run_mock = SimpleNamespace(id=run_id, status="RUNNING", execution_owner=None)
    prepared_run = PreparedTurnRun(
        turn=turn_mock,
        run_id=run_id,
        run=run_mock,
        reused=False,
    )
    thread_service.ensure_stream_run = AsyncMock(return_value=prepared_run)

    request = MagicMock()
    request.app.state.settings.run_execution_in_worker = True
    request.app.state.settings.sse_heartbeat_seconds = 15

    context = SimpleNamespace(
        workspace_id="00000000-0000-0000-0000-000000000010",
        request_id="req-123",
        permissions=frozenset(),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("apps.api.routes.threads._service", lambda req: thread_service)
        mp.setattr(
            "apps.api.routes.threads.get_production_agent_run_service",
            lambda req: FakeRunService(),
        )

        response = await stream_turn(
            workspace_id=UUID("00000000-0000-0000-0000-000000000010"),
            thread_id=UUID("00000000-0000-0000-0000-000000000020"),
            payload=SimpleNamespace(input_text="hello", client_token=None),
            request=request,
            context=context,
            queue=FakeQueue(),
        )

    assert len(enqueued) == 1
    assert enqueued[0][1] == prepared_run.run_id
    assert response.headers["X-AgentHub-Run-Id"] == str(prepared_run.run_id)
