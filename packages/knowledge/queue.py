"""Provider-neutral ingestion queue contract."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class IngestionQueue(Protocol):
    async def enqueue(self, job_id: UUID) -> None:
        """Enqueue only the durable ingestion job identifier."""


__all__ = ["IngestionQueue"]
