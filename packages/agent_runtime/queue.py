"""Durable agent-run queue contract and Celery adapter.

The message carries identifiers only -- never the resolved permissions, never
the input text. The worker re-reads the run and re-derives workspace access
from the database, so a message that is forged, replayed, or merely stale
cannot authorize anything the caller did not already have.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

from celery import Celery

AGENT_RUN_TASK_NAME = "agenthub.execute_agent_run"


class AgentRunQueue(Protocol):
    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        """Transport only the durable run identifiers."""


class CeleryAgentRunQueue:
    def __init__(self, celery_app: Celery) -> None:
        self.celery_app = celery_app

    async def enqueue(self, *, workspace_id: UUID, run_id: UUID, request_id: str) -> None:
        await asyncio.to_thread(
            self.celery_app.send_task,
            AGENT_RUN_TASK_NAME,
            args=[str(workspace_id), str(run_id), request_id],
            ignore_result=True,
        )


__all__ = ["AGENT_RUN_TASK_NAME", "AgentRunQueue", "CeleryAgentRunQueue"]
