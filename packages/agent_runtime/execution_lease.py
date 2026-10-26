"""One durable execution owner per run; lost owners are never blindly replayed."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import case, func, select, update

from packages.agent_runtime.models import AgentRun
from packages.core.errors.exceptions import AgentHubError

LEASE_SECONDS = 180
RENEW_SECONDS = 30


def _live(expiry, now):
    return expiry is not None and (expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)) > now


@asynccontextmanager
async def run_execution_lease(factory, workspace_id, run_id, *, allowed_statuses):
    token = uuid4().hex
    claimed = False
    async with factory() as session:
        run = await session.scalar(
            select(AgentRun)
            .where(AgentRun.workspace_id == workspace_id, AgentRun.id == run_id)
            .with_for_update()
        )
        if run is None:
            raise AgentHubError("AGENT_RUN_NOT_FOUND", "The agent run was not found.", 404)
        now = datetime.now(UTC)
        if run.status in allowed_statuses:
            if run.execution_owner:
                if not _live(run.execution_lease_expires_at, now):
                    run.status = "NEEDS_ATTENTION"
                    run.event_log_complete = False
                    run.failure_code = "RUN_EXECUTION_LEASE_EXPIRED"
                    run.completed_at = now
                    run.execution_owner = None
                    run.execution_lease_expires_at = None
            else:
                run.execution_owner = token
                run.execution_lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
                claimed = True
        await session.commit()
    if not claimed:
        yield None
        return
    owner_task = asyncio.current_task()
    lost = False

    async def renew():
        nonlocal lost
        while True:
            await asyncio.sleep(RENEW_SECONDS)
            try:
                async with factory() as session:
                    result = await session.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.workspace_id == workspace_id,
                            AgentRun.id == run_id,
                            AgentRun.execution_owner == token,
                            AgentRun.execution_lease_expires_at > func.now(),
                        )
                        .values(
                            execution_lease_expires_at=datetime.now(UTC)
                            + timedelta(seconds=LEASE_SECONDS)
                        )
                    )
                    await session.commit()
                    if result.rowcount == 1:
                        continue
            except Exception:
                pass
            lost = True
            try:
                async with factory() as session:
                    await session.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.workspace_id == workspace_id,
                            AgentRun.id == run_id,
                            AgentRun.execution_owner == token,
                        )
                        .values(
                            status="NEEDS_ATTENTION",
                            event_log_complete=False,
                            failure_code="RUN_EXECUTION_LEASE_LOST",
                            completed_at=datetime.now(UTC),
                        )
                    )
                    await session.commit()
            except Exception:
                # Leave the lease in place: the next claimant must fence the
                # expired owner to NEEDS_ATTENTION, never re-execute the graph.
                pass
            owner_task.cancel()
            return

    renewal = asyncio.create_task(renew())
    try:
        yield token
    except asyncio.CancelledError:
        if not lost:
            # Process/request cancellation can interrupt a claimed action.
            # Preserve a reconciliation marker before releasing ownership.
            async with factory() as session:
                await session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.workspace_id == workspace_id,
                        AgentRun.id == run_id,
                        AgentRun.execution_owner == token,
                        AgentRun.status.in_({"RUNNING", "CANCEL_REQUESTED"}),
                    )
                    .values(
                        status="NEEDS_ATTENTION",
                        event_log_complete=False,
                        failure_code="RUN_EXECUTION_INTERRUPTED",
                        completed_at=datetime.now(UTC),
                    )
                )
                await session.commit()
        if lost:
            raise AgentHubError(
                "RUN_EXECUTION_LEASE_LOST",
                "Run ownership was lost; reconciliation is required.",
                409,
            ) from None
        raise
    except BaseException:
        async with factory() as session:
            await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.id == run_id,
                    AgentRun.execution_owner == token,
                    AgentRun.status.in_({"RUNNING", "CANCEL_REQUESTED"}),
                )
                .values(
                    status="NEEDS_ATTENTION",
                    event_log_complete=False,
                    failure_code="RUN_EXECUTION_INTERRUPTED",
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()
        raise
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
        # A newer owner/maintenance transition cannot be cleared by this worker.
        async with factory() as session:
            await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.id == run_id,
                    AgentRun.execution_owner == token,
                )
                .values(
                    execution_owner=None,
                    execution_lease_expires_at=None,
                    event_log_complete=case(
                        (AgentRun.status.in_({"RUNNING", "CANCEL_REQUESTED"}), False),
                        else_=AgentRun.event_log_complete,
                    ),
                    # Release and nonterminal fencing form one transaction.
                    # RUNNING with no owner must remain a safe-to-start draft,
                    # never a partially executed graph whose error write failed.
                    status=case(
                        (AgentRun.status.in_({"RUNNING", "CANCEL_REQUESTED"}), "NEEDS_ATTENTION"),
                        else_=AgentRun.status,
                    ),
                    failure_code=case(
                        (
                            AgentRun.status.in_({"RUNNING", "CANCEL_REQUESTED"}),
                            "RUN_EXECUTION_INTERRUPTED",
                        ),
                        else_=AgentRun.failure_code,
                    ),
                    completed_at=case(
                        (AgentRun.status.in_({"RUNNING", "CANCEL_REQUESTED"}), datetime.now(UTC)),
                        else_=AgentRun.completed_at,
                    ),
                )
            )
            await session.commit()
