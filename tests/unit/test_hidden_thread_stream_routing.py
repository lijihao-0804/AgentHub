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
