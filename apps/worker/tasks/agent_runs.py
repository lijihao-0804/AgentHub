"""Execute an agent run in the worker instead of inside the API request.

Until this task existed, ``apps/worker/tasks/`` held only reconciliation:
knowledge, approvals and evaluation all had a worker, but a *run* was executed
by the HTTP request that asked for it. That made the request the unit of
execution, which is why a client disconnect had to abort the run.

Executing here inverts it. The API prepares the run row and dispatches; the
worker executes and writes the event stream to ``agent_run_events``; the API
follows that log and serves SSE from it. No connection owns the run, so no
disconnect can end it, and any API process can serve any run's stream because
the log -- not a socket, and not a message broker -- is the delivery path.

Two deliberate decisions:

* **A queue message is not an authorization.** The message carries identifiers
  only. Permissions are re-derived here from the database through the same
  ``TenantService.get_workspace_access`` the API uses, so a forged or stale
  message cannot widen what the run may do -- and a membership revoked between
  dispatch and execution is honoured, not ignored.
* **The run is opened detached.** In the API a hub with no subscribers means
  everyone has left and the run should be aborted; here it is the normal state,
  because nobody is watching the worker.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from apps.agent_run_composition import build_agent_run_service
from apps.worker.celery_app import celery_app
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals.models import Approval
from packages.control_plane.services import TenantService
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import PrincipalContext
from packages.memory.queue import CeleryMemoryWriteQueue


@celery_app.task(
    bind=True,
    name="agenthub.execute_agent_run",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_agent_run(_task, workspace_id: str, run_id: str, request_id: str) -> None:
    try:
        parsed_workspace_id = UUID(workspace_id)
        parsed_run_id = UUID(run_id)
    except ValueError:
        return
    asyncio.run(
        _execute_agent_run(
            get_settings(),
            workspace_id=parsed_workspace_id,
            run_id=parsed_run_id,
            request_id=request_id,
        )
    )


@celery_app.task(
    name="agenthub.resume_agent_run", ignore_result=True, acks_late=True, reject_on_worker_lost=True
)
def resume_agent_run(run_id: str, approval_id: str) -> None:
    asyncio.run(_resume_agent_run(get_settings(), UUID(run_id), UUID(approval_id)))


async def _authorized_run_context(session, *, principal, workspace_id):
    try:
        context = (
            await TenantService().get_workspace_access(
                session,
                principal=principal,
                workspace_id=workspace_id,
            )
        ).context
    except AgentHubError as error:
        if error.status_code not in {403, 404}:
            raise
        return None
    if context is None or "agent_run" not in context.permissions:
        return None
    return context


async def _mark_run_access_revoked(session, run) -> None:
    run.status = "NEEDS_ATTENTION"
    run.failure_code = "RUN_ACTOR_ACCESS_REVOKED"
    run.resume_pending = False
    run.event_log_complete = False
    run.completed_at = datetime.now(UTC)
    await session.commit()


async def _resume_agent_run(settings, run_id, approval_id):
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == run_id).with_for_update()
            )
            if run is None or run.status != "WAITING_APPROVAL":
                return
            principal = PrincipalContext(
                request_id=f"resume:{run_id}",
                trace_id=f"resume:{run_id}",
                user_id=str(run.created_by),
            )
            context = await _authorized_run_context(
                session,
                principal=principal,
                workspace_id=run.workspace_id,
            )
            if context is None:
                await _mark_run_access_revoked(session, run)
                return
        await _build_service(settings, factory).resume(
            context, run_id=run_id, approval_id=approval_id
        )
    finally:
        await engine.dispose()


async def _execute_agent_run(
    settings: Settings,
    *,
    workspace_id: UUID,
    run_id: UUID,
    request_id: str,
) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as session:
            run = await session.scalar(
                select(AgentRun)
                .where(AgentRun.workspace_id == workspace_id, AgentRun.id == run_id)
                .with_for_update()
            )
            if run is not None and run.status == "CANCEL_REQUESTED":
                if not run.execution_owner:
                    claim = await session.scalar(
                        select(Approval.id)
                        .where(
                            Approval.workspace_id == workspace_id,
                            Approval.run_id == run_id,
                            Approval.execution_status.in_({"CLAIMED", "UNKNOWN_OUTCOME"}),
                        )
                        .limit(1)
                    )
                    run.status = "NEEDS_ATTENTION" if isinstance(claim, UUID) else "CANCELLED"
                    run.event_log_complete = False
                    run.failure_code = (
                        "ACTION_RECONCILIATION_REQUIRED" if isinstance(claim, UUID) else None
                    )
                    run.completed_at = datetime.now(UTC)
                    await session.commit()
                return
            if run is None or run.status != "RUNNING":
                # Already executed, cancelled, or reconciled away. Re-running
                # would duplicate side effects that the run may already have
                # committed, so the safe response to an ambiguous redelivery is
                # to do nothing.
                return
            agent_version_id = run.agent_version_id
            input_text = run.input_text
            principal = PrincipalContext(
                request_id=request_id,
                trace_id=request_id,
                user_id=str(run.created_by),
            )
            context = await _authorized_run_context(
                session,
                principal=principal,
                workspace_id=workspace_id,
            )
            if context is None:
                await _mark_run_access_revoked(session, run)
                return
            # Detach with its attributes already loaded: the run object outlives
            # this session, and a lazy refresh on a closed session would fail.
            session.expunge(run)

        service = _build_service(settings, factory)
        hub = await service.open_stream(
            context,
            agent_version_id=agent_version_id,
            input_text=input_text,
            prepared_run=run,
            detached=True,
        )
        await hub.wait_closed()
    finally:
        await engine.dispose()


def _build_service(settings: Settings, factory) -> AgentRunService:
    return build_agent_run_service(
        settings, factory, memory_writer=CeleryMemoryWriteQueue(celery_app)
    )


__all__ = ["execute_agent_run"]
