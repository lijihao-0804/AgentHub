"""The hand-off between a finished run and memory extraction.

Same contract as ``packages.agent_runtime.queue``: the message carries
identifiers only. The worker re-reads the run, re-derives workspace access from
the database and re-checks the published version's memory switch, so a replayed
or forged message cannot cause an agent to learn anything it was not configured
to learn.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from celery import Celery

MEMORY_EXTRACTION_TASK_NAME = "agenthub.extract_run_memories"


class CeleryMemoryWriteQueue:
    def __init__(self, celery_app: Celery) -> None:
        self.celery_app = celery_app

    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        await asyncio.to_thread(
            self.celery_app.send_task,
            MEMORY_EXTRACTION_TASK_NAME,
            args=[str(workspace_id), str(run_id), request_id],
            ignore_result=True,
        )


__all__ = ["MEMORY_EXTRACTION_TASK_NAME", "CeleryMemoryWriteQueue"]
