"""Durable evaluation-run queue contract and Celery adapter."""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from celery import Celery

EXPERIMENT_RUN_TASK_NAME = "agenthub.execute_experiment_run"


class ExperimentRunQueue(Protocol):
    async def enqueue(self, run_id: UUID) -> None:
        """Transport only the durable run identifier."""


class CeleryExperimentRunQueue:
    def __init__(self, celery_app: Celery) -> None:
        self.celery_app = celery_app

    async def enqueue(self, run_id: UUID) -> None:
        await asyncio.to_thread(
            self.celery_app.send_task,
            EXPERIMENT_RUN_TASK_NAME,
            args=[str(run_id)],
            ignore_result=True,
        )


__all__ = ["CeleryExperimentRunQueue", "EXPERIMENT_RUN_TASK_NAME", "ExperimentRunQueue"]
