from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.threads.service import ThreadService

_THREAD_ID = UUID("00000000-0000-0000-0000-000000000101")
_TURN_ID = UUID("00000000-0000-0000-0000-000000000102")
_LATEST_VERSION_ID = UUID("00000000-0000-0000-0000-000000000103")
_RUN_VERSION_ID = UUID("00000000-0000-0000-0000-000000000104")
_RUN_ID = UUID("00000000-0000-0000-0000-000000000105")


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="request-1", trace_id="trace-1", user_id="user-1"
            ),
            organization_id="org-1",
            org_role="MEMBER",
        ),
        workspace_id="workspace-1",
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run", "workspace_read"}),
    )


class _RunService:
    async def get_run(self, _context, run_id):
        assert run_id == _RUN_ID
        return SimpleNamespace(agent_version_id=_RUN_VERSION_ID)


class _RetryService(ThreadService):
    def __init__(self, existing_turn):
        super().__init__(session_factory=None, run_service=_RunService())
        self.existing_turn = existing_turn

    async def resolve_agent_version(self, *_args, **_kwargs):
        return SimpleNamespace(id=_THREAD_ID), _LATEST_VERSION_ID

    async def open_turn(self, *_args, **_kwargs):
        return None

    async def _require_token_turn(self, *_args, **_kwargs):
        return self.existing_turn


@pytest.mark.asyncio
async def test_duplicate_submission_before_run_attachment_returns_retryable_conflict() -> None:
    service = _RetryService(SimpleNamespace(id=_TURN_ID, agent_run_id=None))

    with pytest.raises(AgentHubError) as raised:
        await service.submit_turn(
            _context(), _THREAD_ID, user_input="question", client_token="token-1"
        )

    assert raised.value.code == "THREAD_TURN_IN_PROGRESS"
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_attached_duplicate_returns_the_original_run_and_version() -> None:
    service = _RetryService(SimpleNamespace(id=_TURN_ID, agent_run_id=_RUN_ID))

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True
    assert submitted.turn.id == _TURN_ID
    assert submitted.run_id == _RUN_ID
    assert submitted.agent_version_id == _RUN_VERSION_ID


class _RecoveringRunService(_RunService):
    """Fake run service whose ``run`` records the re-run input."""

    def __init__(self, run_status: str | None = None):
        self.run_status = run_status
        self.rerun_input: str | None = None

    async def run(self, _context, *, agent_version_id, input_text, thread_id=None):
        self.rerun_input = input_text
        return SimpleNamespace(run_id=_RUN_ID, agent_version_id=agent_version_id)


class _OrphanRecoveryService(_RetryService):
    """Retries past the grace window with a controllable orphan-run probe."""

    def __init__(self, existing_turn, run_service, turn_run=None):
        super().__init__(existing_turn)
        self.run_service = run_service
        self.turn_run = turn_run
        self.attached: tuple[UUID, UUID] | None = None

    async def _turn_run(self, _context, _thread_id, _turn):
        return self.turn_run

    async def attach_run(self, _context, *, turn_id, run_id):
        self.attached = (turn_id, run_id)


def _stale_turn() -> SimpleNamespace:
    return SimpleNamespace(
        id=_TURN_ID,
        agent_run_id=None,
        created_at=datetime.now(UTC) - timedelta(seconds=600),
        user_input="question",
    )


@pytest.mark.asyncio
async def test_settled_run_created_after_turn_is_attached_on_retry() -> None:
    """The first request died between run completion and attach_run: the retry
    attaches the settled run instead of answering 409 forever."""

    turn_run = SimpleNamespace(
        id=_RUN_ID, status="SUCCEEDED", agent_version_id=_RUN_VERSION_ID
    )
    service = _OrphanRecoveryService(_stale_turn(), _RecoveringRunService(), turn_run)

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True
    assert submitted.run_id == _RUN_ID
    assert submitted.agent_version_id == _RUN_VERSION_ID
    assert service.attached == (_TURN_ID, _RUN_ID)


@pytest.mark.asyncio
async def test_orphan_turn_past_grace_window_reruns_the_question() -> None:
    """No run was ever created and the turn is past the start grace window:
    the retry re-runs the recorded question instead of 409ing forever."""

    run_service = _RecoveringRunService()
    service = _OrphanRecoveryService(_stale_turn(), run_service, turn_run=None)

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True
    assert submitted.run_id == _RUN_ID
    assert run_service.rerun_input == "question"
    assert service.attached == (_TURN_ID, _RUN_ID)


@pytest.mark.asyncio
async def test_inflight_run_created_after_turn_keeps_retryable_conflict() -> None:
    """A still-running run belongs to a live first request: the retry must
    keep waiting rather than double-running the question."""

    turn_run = SimpleNamespace(
        id=_RUN_ID, status="RUNNING", agent_version_id=_RUN_VERSION_ID
    )
    service = _OrphanRecoveryService(_stale_turn(), _RecoveringRunService(), turn_run)

    with pytest.raises(AgentHubError) as raised:
        await service.submit_turn(
            _context(), _THREAD_ID, user_input="question", client_token="token-1"
        )

    assert raised.value.code == "THREAD_TURN_IN_PROGRESS"
    assert raised.value.status_code == 409
