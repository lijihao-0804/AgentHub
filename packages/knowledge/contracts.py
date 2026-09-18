from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from packages.core.execution_context.models import WorkspaceExecutionContext


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    knowledge_base_id: str
    knowledge_snapshot_id: str
    dense_top_k: int = 30
    sparse_top_k: int = 30
    candidate_top_k: int = 20
    final_top_k: int = 6


@dataclass(frozen=True)
class RetrievedEvidence:
    document_id: str
    document_revision_id: str
    chunk_id: str
    source: str
    locator: dict[str, Any]
    text: str
    retrieval_score: float
    rerank_score: float | None
    metadata: dict[str, Any]


class KnowledgeRetriever(Protocol):
    async def retrieve(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> list[RetrievedEvidence]: ...
