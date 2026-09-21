from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from packages.core.execution_context.models import WorkspaceExecutionContext


class RetrievalStrategy(StrEnum):
    DENSE = "DENSE"
    HYBRID = "HYBRID"
    HYBRID_RERANK = "HYBRID_RERANK"


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    knowledge_base_id: str
    knowledge_snapshot_id: str
    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID_RERANK
    dense_top_k: int = 30
    sparse_top_k: int = 30
    candidate_top_k: int = 20
    final_top_k: int = 6
    # Per-query override of the configured relevance floor. ``None`` means
    # "use the retriever's configured value"; it does not mean "no floor".
    min_rerank_score: float | None = None


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
class RetrievalTraceResult:
    """Provider-neutral result projection for one retrieval stage."""

    chunk_id: str
    rank: int
    score: float


@dataclass(frozen=True)
class RetrievalTraceStage:
    latency_ms: float
    results: tuple[RetrievalTraceResult, ...]


@dataclass(frozen=True)
class RetrievalTrace:
    """Safe retrieval trace data; document and chunk text are intentionally absent."""

    snapshot_id: str
    dense: RetrievalTraceStage
    sparse: RetrievalTraceStage
    fusion: RetrievalTraceStage
    rerank: RetrievalTraceStage
    total_latency_ms: float
    # How many reranked chunks the relevance floor removed. Without this an
    # empty evidence set is indistinguishable from an empty index, which is
    # the one question someone looking at a trace will actually have.
    dropped_below_floor: int = 0


@dataclass(frozen=True)
class RetrievalResult:
    evidence: tuple[RetrievedEvidence, ...]
    trace: RetrievalTrace


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


RETRYABLE_PROVIDER_ERROR_CODES = frozenset(
    {
        "QDRANT_UNAVAILABLE",
        "EMBEDDER_LOAD_FAILED",
        "EMBEDDER_INFERENCE_FAILED",
        "SPARSE_TOKENIZER_LOAD_FAILED",
        "SPARSE_ENCODING_FAILED",
        "RERANKER_LOAD_FAILED",
        "RERANKER_INFERENCE_FAILED",
    }
)

TERMINAL_PROVIDER_ERROR_CODES = frozenset(
    {
        "CUDA_UNAVAILABLE",
        "EMBEDDER_DIMENSION_MISMATCH",
        "EMBEDDER_INVALID_VECTOR",
        "INVALID_DENSE_VECTOR",
        "INVALID_SPARSE_VECTOR",
        "QDRANT_SCHEMA_MISMATCH",
        "INVALID_PROVIDER_RESULT",
        "EMBEDDER_UNAVAILABLE",
        "SPARSE_ENCODER_UNAVAILABLE",
        "RERANKER_UNAVAILABLE",
        "INVALID_RERANK_RESULT",
    }
)


def provider_error_is_retryable(code: str) -> bool:
    """Unknown provider codes are terminal until explicitly classified."""

    return code in RETRYABLE_PROVIDER_ERROR_CODES


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

    async def retrieve_with_trace(
        self,
        context: WorkspaceExecutionContext,
        query: RetrievalQuery,
    ) -> RetrievalResult: ...
