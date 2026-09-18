from __future__ import annotations

import asyncio
from uuid import UUID

from celery import Celery

from packages.knowledge.queue import IngestionQueue

INGESTION_TASK_NAME = "agenthub.process_knowledge_ingestion"


class CeleryIngestionQueue(IngestionQueue):
    """Celery adapter that transports only a durable job id."""

    def __init__(self, celery_app: Celery) -> None:
        self.celery_app = celery_app

    async def enqueue(self, job_id: UUID) -> None:
        await asyncio.to_thread(
            self.celery_app.send_task,
            INGESTION_TASK_NAME,
            args=[str(job_id)],
            ignore_result=True,
        )


__all__ = ["CeleryIngestionQueue", "INGESTION_TASK_NAME"]
