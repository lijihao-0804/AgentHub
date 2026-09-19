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
        service = ApprovalReconciliationService(
            factory, checkpoint_probe=PostgresCheckpointProbe(adapter)
        )
        await service.reconcile_candidates(
            batch_size=settings.approval_reconciliation_batch_size,
            stale_after_seconds=settings.approval_reconciliation_stale_seconds,
        )
    finally:
        await engine.dispose()


__all__ = ["reconcile_approval_runs"]
