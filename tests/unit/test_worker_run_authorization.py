"""First execution and resume apply the same Run authorization contract."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.worker.tasks import agent_runs as worker
from packages.core.errors.exceptions import AgentHubError

_REQUEST_ID = "worker-run-request"
_APPROVAL_ID = uuid4()


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _Session:
    def __init__(self, run) -> None:
        self.run = run
        self.commit_calls = 0
        self.expunged = []

    async def scalar(self, _statement):
        return self.run

    async def commit(self) -> None:
        self.commit_calls += 1

    def expunge(self, value) -> None:
        self.expunged.append(value)


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc_info):
        return None


class _Hub:
    def __init__(self) -> None:
        self.closed = False

    async def wait_closed(self) -> None:
        self.closed = True


class _Service:
    def __init__(self) -> None:
        self.opened = []
        self.resumed = []
        self.hub = _Hub()

    async def open_stream(self, context, **kwargs):
        self.opened.append({"context": context, **kwargs})
        return self.hub

    async def resume(self, context, **kwargs):
        self.resumed.append({"context": context, **kwargs})


def _run(*, status: str):
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        agent_version_id=uuid4(),
        input_text="question",
        created_by=uuid4(),
        status=status,
        execution_owner=None,
        failure_code=None,
        resume_pending=True,
        event_log_complete=True,
        completed_at=None,
    )


def _install_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run,
    tenant_result=None,
    tenant_error: AgentHubError | None = None,
    service: _Service | None = None,
):
    engine = _Engine()
    session = _Session(run)

    def factory():
        return _SessionContext(session)

    tenant_calls = []
    service_calls = []

    class Tenant:
        async def get_workspace_access(
            self,
            actual_session,
            *,
            principal,
            workspace_id,
        ):
            tenant_calls.append(
                {
                    "session": actual_session,
                    "principal": principal,
                    "workspace_id": workspace_id,
                }
            )
            if tenant_error is not None:
                raise tenant_error
            return SimpleNamespace(context=tenant_result)

    def build_service(*args, **kwargs):
        service_calls.append((args, kwargs))
        if service is None:
            raise AssertionError("revoked actors must not initialize the run runtime")
        return service

    monkeypatch.setattr(worker, "create_database", lambda _url: (engine, factory))
    monkeypatch.setattr(worker, "TenantService", Tenant)
    monkeypatch.setattr(worker, "_build_service", build_service)
    return engine, session, tenant_calls, service_calls


async def _invoke(target: str, run) -> None:
    settings = SimpleNamespace(database_url="postgresql+asyncpg://unused/test")
    if target == "execute":
        await worker._execute_agent_run(
            settings,
            workspace_id=run.workspace_id,
            run_id=run.id,
            request_id=_REQUEST_ID,
        )
    else:
        await worker._resume_agent_run(settings, run.id, _APPROVAL_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "initial_status"),
    (("execute", "RUNNING"), ("resume", "WAITING_APPROVAL")),
)
async def test_worker_authorized_context_still_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    initial_status: str,
) -> None:
    run = _run(status=initial_status)
    service = _Service()
    authorized_context = SimpleNamespace(permissions=frozenset({"agent_run"}))
    engine, session, _, service_calls = _install_dependencies(
        monkeypatch,
        run=run,
        tenant_result=authorized_context,
        service=service,
    )

    await _invoke(target, run)

    assert run.status == initial_status
    assert session.commit_calls == 0
    assert len(service_calls) == 1
    if target == "execute":
        assert service.opened == [
            {
                "context": authorized_context,
                "agent_version_id": run.agent_version_id,
                "input_text": run.input_text,
                "prepared_run": run,
                "detached": True,
            }
        ]
        assert service.hub.closed is True
        assert service.resumed == []
        assert session.expunged == [run]
    else:
        assert service.opened == []
        assert service.resumed == [
            {
                "context": authorized_context,
                "run_id": run.id,
                "approval_id": _APPROVAL_ID,
            }
        ]
    assert engine.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "initial_status"),
    (("execute", "RUNNING"), ("resume", "WAITING_APPROVAL")),
)
@pytest.mark.parametrize("status_code", (403, 404))
async def test_worker_access_lookup_revocation_is_terminal_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    initial_status: str,
    status_code: int,
) -> None:
    run = _run(status=initial_status)
    engine, session, tenant_calls, service_calls = _install_dependencies(
        monkeypatch,
        run=run,
        tenant_error=AgentHubError("WORKSPACE_ACCESS_DENIED", "denied", status_code),
    )

    await _invoke(target, run)

    expected_request_id = _REQUEST_ID if target == "execute" else f"resume:{run.id}"
    assert tenant_calls[0]["principal"].request_id == expected_request_id
    assert tenant_calls[0]["workspace_id"] == run.workspace_id
    assert run.status == "NEEDS_ATTENTION"
    assert run.failure_code == "RUN_ACTOR_ACCESS_REVOKED"
    assert run.resume_pending is False
    assert run.event_log_complete is False
    assert isinstance(run.completed_at, datetime)
    assert session.commit_calls == 1
    assert session.expunged == []
    assert service_calls == []
    assert engine.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "initial_status"),
    (("execute", "RUNNING"), ("resume", "WAITING_APPROVAL")),
)
async def test_worker_missing_agent_run_permission_is_terminal_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    initial_status: str,
) -> None:
    run = _run(status=initial_status)
    engine, session, _, service_calls = _install_dependencies(
        monkeypatch,
        run=run,
        tenant_result=SimpleNamespace(permissions=frozenset({"workspace_read"})),
    )

    await _invoke(target, run)

    assert run.status == "NEEDS_ATTENTION"
    assert run.failure_code == "RUN_ACTOR_ACCESS_REVOKED"
    assert run.event_log_complete is False
    assert session.commit_calls == 1
    assert service_calls == []
    assert engine.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "initial_status"),
    (("execute", "RUNNING"), ("resume", "WAITING_APPROVAL")),
)
async def test_worker_rethrows_other_agenthub_errors_without_mutating_the_run(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    initial_status: str,
) -> None:
    run = _run(status=initial_status)
    error = AgentHubError("DATABASE_UNAVAILABLE", "database unavailable", 503)
    engine, session, _, service_calls = _install_dependencies(
        monkeypatch,
        run=run,
        tenant_error=error,
    )

    with pytest.raises(AgentHubError) as raised:
        await _invoke(target, run)

    assert raised.value is error
    assert run.status == initial_status
    assert run.failure_code is None
    assert run.event_log_complete is True
    assert session.commit_calls == 0
    assert service_calls == []
    assert engine.disposed is True
