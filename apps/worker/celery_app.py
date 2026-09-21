from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_shutdown

from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.core.config.settings import Settings, get_settings
from packages.knowledge.composition import close_production_retrieval_components


def create_celery_app(settings: Settings | None = None) -> Celery:
    configure_windows_asyncio_policy()
    app_settings = settings or get_settings()
    app = Celery(
        "agenthub",
        broker=app_settings.redis_url,
        include=[
            "apps.worker.tasks.agent_runs",
            "apps.worker.tasks.knowledge",
            "apps.worker.tasks.approvals",
            "apps.worker.tasks.evaluation",
            "apps.worker.tasks.memories",
        ],
    )
    app.conf.update(
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=False,
        enable_utc=True,
        timezone="UTC",
        beat_schedule={
            "reconcile-knowledge-ingestion": {
                "task": "agenthub.reconcile_knowledge_ingestion",
                "schedule": 60.0,
            },
            "reconcile-approval-runs": {
                "task": "agenthub.reconcile_approval_runs",
                "schedule": 30.0,
            },
            "reconcile-evaluation-runs": {
                "task": "agenthub.reconcile_evaluation_runs",
                "schedule": 60.0,
            },
        },
    )
    return app


celery_app = create_celery_app()


@worker_process_shutdown.connect
def _close_worker_retrieval_components(**_kwargs) -> None:
    close_production_retrieval_components()


__all__ = ["celery_app", "create_celery_app"]
