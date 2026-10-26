"""Periodic durable Approval/Run reconciliation owned by the worker."""

from __future__ import annotations

import asyncio

from apps.worker.celery_app import celery_app
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    PostgresCheckpointProbe,
)
from packages.approvals.reconciliation import ApprovalReconciliationService
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database


@celery_app.task(
    bind=True,
    name="agenthub.reconcile_approval_runs",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_approval_runs(_task) -> None:
    asyncio.run(_reconcile_approval_runs(get_settings()))


async def _reconcile_approval_runs(settings: Settings) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        adapter = LangGraphCheckpointAdapter(settings.database_url)

        async def dispatch(run, approval):
            from apps.worker.tasks.agent_runs import resume_agent_run

            # The run.resume_pending outbox stays set until the owner finishes
            # resuming. Queue loss is retried by the next reconciliation pass.
            await asyncio.to_thread(resume_agent_run.delay, str(run.id), str(approval.id))

        service = ApprovalReconciliationService(
            factory,
            checkpoint_probe=PostgresCheckpointProbe(adapter),
            resume_dispatch=dispatch,
        )
        await service.reconcile_candidates(
            batch_size=settings.approval_reconciliation_batch_size,
            stale_after_seconds=settings.approval_reconciliation_stale_seconds,
        )
    finally:
        await engine.dispose()


__all__ = ["reconcile_approval_runs"]
