from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    workspace_id: str
    candidate_top_k: int = 20
    final_top_k: int = 6


@dataclass(frozen=True)
class RetrievedEvidence:
    document_revision_id: str
    locator: dict[str, Any]
    text: str
    score: float


class KnowledgeRetriever(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> list[RetrievedEvidence]: ...
