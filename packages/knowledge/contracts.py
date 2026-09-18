from __future__ import annotations

from collections.abc import Sequence
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


@dataclass(frozen=True)
class SparseEncoding:
    """Provider-neutral sparse vector with deterministic, sorted coordinates."""

    indices: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: str
    text: str


@dataclass(frozen=True)
class VectorRecord:
    point_id: str
    chunk_id: str
    dense: tuple[float, ...]
    sparse: SparseEncoding
    payload: dict[str, Any]


@dataclass(frozen=True)
class VectorScope:
    workspace_id: str
    knowledge_base_id: str
    document_revision_ids: tuple[str, ...]


@dataclass(frozen=True)
class VectorSearchHit:
    point_id: str
    score: float
    payload: dict[str, Any]


class KnowledgeProviderError(Exception):
    """Safe provider failure that can be mapped without exposing raw exceptions."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DenseEmbedder(Protocol):
    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


class SparseEncoder(Protocol):
    def encode_documents(self, texts: Sequence[str]) -> tuple[SparseEncoding, ...]: ...

    def encode_query(self, text: str) -> SparseEncoding: ...


class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> tuple[float, ...]: ...


class VectorIndex(Protocol):
    def ensure_collection(self) -> None: ...

    def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    def dense_search(
        self,
        query: Sequence[float],
        *,
        scope: VectorScope,
        limit: int,
    ) -> tuple[VectorSearchHit, ...]: ...

    def sparse_search(
        self,
        query: SparseEncoding,
        *,
        scope: VectorScope,
        limit: int,
    ) -> tuple[VectorSearchHit, ...]: ...


class KnowledgeRetriever(Protocol):
    async def retrieve(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> list[RetrievedEvidence]: ...
