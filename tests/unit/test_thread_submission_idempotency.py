from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.threads.service import ThreadService

_THREAD_ID = UUID("00000000-0000-0000-0000-000000000101")
_WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000100")
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
        workspace_id=str(_WORKSPACE_ID),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run", "workspace_read"}),
    )


class _RunService:
    def __init__(self) -> None:
        self.executed: list[UUID] = []

    async def get_run(self, _context, run_id):
        assert run_id == _RUN_ID
        return SimpleNamespace(agent_version_id=_RUN_VERSION_ID)

    async def execute_prepared_run(self, _context, *, run_id):
        self.executed.append(run_id)
        return SimpleNamespace(run_id=run_id)


class _RetryService(ThreadService):
    def __init__(self, existing_turn):
        super().__init__(session_factory=None, run_service=_RunService())
        self.existing_turn = existing_turn

    async def resolve_agent_version(self, *_args, **_kwargs):
        return SimpleNamespace(id=_THREAD_ID), _LATEST_VERSION_ID

    async def open_turn(self, *_args, **_kwargs):
        return self.existing_turn

    async def _require_token_turn(self, *_args, **_kwargs):
        return self.existing_turn


@pytest.mark.asyncio
async def test_attached_duplicate_returns_the_original_run_and_version() -> None:
    turn_run = SimpleNamespace(
        id=_RUN_ID,
        agent_version_id=_RUN_VERSION_ID,
        input_text="question",
    )
    service = _OrphanRecoveryService(
        _turn(agent_run_id=_RUN_ID),
        _PreparingRunService(),
        turn_run,
    )

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True
    assert submitted.turn.id == _TURN_ID
    assert submitted.run_id == _RUN_ID
    assert submitted.agent_version_id == _RUN_VERSION_ID


class _PreparingRunService(_RunService):
    """Fake run service proving an orphan turn gets a stable Run identity."""

    def __init__(self, run_status: str | None = None):
        super().__init__()
        self.run_status = run_status
        self.prepared: dict[str, object] | None = None

    async def prepare_run(
        self,
        _context,
        *,
        agent_version_id,
        input_text,
        thread_id=None,
        thread_turn_id=None,
    ):
        self.prepared = {
            "agent_version_id": agent_version_id,
            "input_text": input_text,
            "thread_id": thread_id,
            "thread_turn_id": thread_turn_id,
        }
        return SimpleNamespace(id=_RUN_ID, agent_version_id=agent_version_id)


class _OrphanRecoveryService(_RetryService):
    """Exercises the service path with a controllable legacy/Run probe."""

    def __init__(self, existing_turn, run_service, turn_run=None):
        super().__init__(existing_turn)
        self.run_service = run_service
        self.turn_run = turn_run
        self.attached: tuple[UUID, UUID] | None = None

    async def _turn_run(self, _context, _thread_id, _turn):
        return self.turn_run

    async def attach_run(self, _context, *, turn_id, run_id):
        self.attached = (turn_id, run_id)


def _turn(*, legacy_recovery_required=False, agent_run_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=_TURN_ID,
        agent_run_id=agent_run_id,
        user_input="question",
        legacy_recovery_required=legacy_recovery_required,
    )


@pytest.mark.asyncio
async def test_explicit_bound_run_is_reused_without_executing_again() -> None:
    """A stable binding is proof; retries do not infer or re-run the question."""

    turn_run = SimpleNamespace(
        id=_RUN_ID,
        status="SUCCEEDED",
        agent_version_id=_RUN_VERSION_ID,
        input_text="question",
    )
    service = _OrphanRecoveryService(_turn(), _PreparingRunService(), turn_run)

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True and submitted.run_id == _RUN_ID
    assert submitted.agent_version_id == _RUN_VERSION_ID
    assert service.attached == (_TURN_ID, _RUN_ID)
    assert service.run_service.prepared is None


@pytest.mark.asyncio
async def test_prepared_run_without_an_owner_is_executed_on_retry() -> None:
    """A crash after prepare/attach must not strand an unclaimed RUNNING Run."""

    turn_run = SimpleNamespace(
        id=_RUN_ID,
        status="RUNNING",
        execution_owner=None,
        agent_version_id=_RUN_VERSION_ID,
        input_text="question",
    )
    run_service = _PreparingRunService()
    service = _OrphanRecoveryService(_turn(agent_run_id=_RUN_ID), run_service, turn_run)

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True and submitted.run_id == _RUN_ID
    assert run_service.executed == [_RUN_ID]
    assert run_service.prepared is None


@pytest.mark.asyncio
async def test_prepared_run_with_an_owner_is_only_observed_on_retry() -> None:
    """A retry cannot take over a live or expired owner's execution."""

    turn_run = SimpleNamespace(
        id=_RUN_ID,
        status="RUNNING",
        execution_owner="worker-with-an-expired-lease",
        agent_version_id=_RUN_VERSION_ID,
        input_text="question",
    )
    run_service = _PreparingRunService()
    service = _OrphanRecoveryService(_turn(agent_run_id=_RUN_ID), run_service, turn_run)

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is True and submitted.run_id == _RUN_ID
    assert run_service.executed == []
    assert run_service.prepared is None


@pytest.mark.asyncio
async def test_new_unbound_turn_prepares_a_run_with_stable_turn_identity() -> None:
    """A new Turn with no Run is safe to prepare; no time heuristic is used."""

    run_service = _PreparingRunService()
    service = _OrphanRecoveryService(_turn(), run_service, turn_run=None)

    submitted = await service.submit_turn(
        _context(), _THREAD_ID, user_input="question", client_token="token-1"
    )

    assert submitted.reused is False and submitted.run_id == _RUN_ID
    assert run_service.prepared == {
        "agent_version_id": _LATEST_VERSION_ID,
        "input_text": "question",
        "thread_id": _THREAD_ID,
        "thread_turn_id": _TURN_ID,
    }
    assert service.attached == (_TURN_ID, _RUN_ID)


@pytest.mark.asyncio
async def test_legacy_ambiguous_turn_requires_recovery_without_preparing() -> None:
    """An old Turn without a proven one-to-one backlink fails closed."""

    class _LegacyRunService(_OrphanRecoveryService):
        async def _turn_run(self, _context, _thread_id, turn):
            if turn.legacy_recovery_required:
                raise AgentHubError(
                    "THREAD_TURN_RECOVERY_REQUIRED",
                    "The historical turn cannot be recovered without operator review.",
                    409,
                )
            return None

    run_service = _PreparingRunService()
    service = _LegacyRunService(
        _turn(legacy_recovery_required=True),
        run_service,
        turn_run=None,
    )

    with pytest.raises(AgentHubError) as raised:
        await service.submit_turn(
            _context(), _THREAD_ID, user_input="question", client_token="token-1"
        )

    assert raised.value.code == "THREAD_TURN_RECOVERY_REQUIRED"
    assert raised.value.status_code == 409
    assert run_service.prepared is None


@pytest.mark.asyncio
async def test_only_turn_uniqueness_integrity_error_is_recovered() -> None:
    turn = _turn()
    service = _RetryService(turn)

    other = IntegrityError("statement", {}, Exception("different constraint"))
    with pytest.raises(IntegrityError):
        await service._winner_after_run_conflict(
            _context(), thread_id=_THREAD_ID, turn=turn, error=other
        )


@pytest.mark.asyncio
async def test_old_turn_reverse_pointer_cannot_disagree_with_an_input_mismatch() -> None:
    """A stale Run backlink is recovery-required, never silently replaced."""

    class _Session:
        def __init__(self, stable):
            self.stable = stable
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            self.calls += 1
            return self.stable if self.calls == 1 else None

    turn = _turn(agent_run_id=_RUN_ID)
    stable = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000999"),
        input_text="question",
    )
    service = ThreadService(lambda: _Session(stable))

    with pytest.raises(AgentHubError) as raised:
        await service._turn_run(_context(), _THREAD_ID, turn)

    assert raised.value.code == "THREAD_TURN_RECOVERY_REQUIRED"
    assert raised.value.status_code == 409
