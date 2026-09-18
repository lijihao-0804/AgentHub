from __future__ import annotations

from celery import Celery

from packages.core.config.settings import Settings, get_settings


def create_celery_app(settings: Settings | None = None) -> Celery:
    app_settings = settings or get_settings()
    app = Celery(
        "agenthub",
        broker=app_settings.redis_url,
        include=["apps.worker.tasks.knowledge"],
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
            }
        },
    )
    return app


celery_app = create_celery_app()


__all__ = ["celery_app", "create_celery_app"]
