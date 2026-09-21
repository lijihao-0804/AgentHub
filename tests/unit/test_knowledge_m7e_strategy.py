from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalStrategy,
    SparseEncoding,
    VectorSearchHit,
)
from packages.knowledge.retrieval import HybridKnowledgeRetriever


class _Dense:
    def __init__(self) -> None:
        self.calls = 0

    def embed_query(self, _text: str) -> tuple[float, ...]:
        self.calls += 1
        return (1.0,)

    def embed_documents(self, _texts):
        return ()

    @property
    def dimension(self) -> int:
        return 1


class _Sparse:
    def __init__(self) -> None:
        self.calls = 0

    def encode_query(self, _text: str) -> SparseEncoding:
        self.calls += 1
        return SparseEncoding((1,), (1.0,))

    def encode_documents(self, _texts):
        return ()


class _Reranker:
    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, _query: str, candidates) -> tuple[float, ...]:
        self.calls += 1
        return tuple(float(len(candidates) - index) for index, _ in enumerate(candidates))


class _Index:
    def __init__(self) -> None:
        self.dense_calls = 0
        self.sparse_calls = 0

    def dense_search(self, _query, *, scope, limit: int):
        self.dense_calls += 1
        return (
            VectorSearchHit("p1", 0.9, {"chunk_id": "c1"}),
            VectorSearchHit("p2", 0.8, {"chunk_id": "c2"}),
        )[:limit]

    def sparse_search(self, _query, *, scope, limit: int):
        self.sparse_calls += 1
        return (
            VectorSearchHit("p2", 0.95, {"chunk_id": "c2"}),
            VectorSearchHit("p3", 0.7, {"chunk_id": "c3"}),
        )[:limit]


def _context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        permissions=frozenset({"knowledge_run"}),
    )


def _query(strategy: RetrievalStrategy) -> RetrievalQuery:
    return RetrievalQuery(
        text="reset password",
        knowledge_base_id=str(uuid4()),
        knowledge_snapshot_id=str(uuid4()),
        strategy=strategy,
        final_top_k=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "sparse_calls", "reranker_calls", "fusion_results", "rerank_results"),
    [
        (RetrievalStrategy.DENSE, 0, 0, 0, 0),
        (RetrievalStrategy.HYBRID, 1, 0, 3, 0),
        (RetrievalStrategy.HYBRID_RERANK, 1, 1, 3, 2),
    ],
)
async def test_retrieval_strategy_controls_provider_calls_and_trace(
    strategy: RetrievalStrategy,
    sparse_calls: int,
    reranker_calls: int,
    fusion_results: int,
    rerank_results: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dense = _Dense()
    sparse = _Sparse()
    reranker = _Reranker()
    index = _Index()
    retriever = HybridKnowledgeRetriever(
        session=object(),
        dense_embedder=dense,
        sparse_encoder=sparse,
        reranker=reranker,
        vector_index=index,
    )

    async def snapshot_scope(_session, _context, _query):
        return uuid4(), uuid4(), (str(uuid4()),)

    async def load_chunks(_session, **_kwargs):
        return {
            chunk_id: (
                SimpleNamespace(
                    chunk_id=chunk_id,
                    text=f"text-{chunk_id}",
                    locator={"page": 1},
                    ordinal=1,
                    normalized_content_hash="hash",
                ),
                SimpleNamespace(
                    id=uuid4(),
                    name=f"document-{chunk_id}.md",
                    effective_date=None,
                    superseded_by_document_id=None,
                ),
                SimpleNamespace(id=uuid4()),
            )
            for chunk_id in ("c1", "c2", "c3")
        }

    monkeypatch.setattr(retriever, "_snapshot_scope", snapshot_scope)
    monkeypatch.setattr(retriever, "_load_chunks", load_chunks)

    result = await retriever.retrieve_with_trace(_context(), _query(strategy))

    assert dense.calls == 1
    assert index.dense_calls == 1
    assert index.sparse_calls == sparse_calls
    assert sparse.calls == sparse_calls
    assert reranker.calls == reranker_calls
    assert len(result.trace.fusion.results) == fusion_results
    assert len(result.trace.rerank.results) == rerank_results
    if strategy is RetrievalStrategy.DENSE:
        assert result.trace.sparse.latency_ms == 0
        assert result.trace.sparse.results == ()
        assert result.trace.fusion.latency_ms == 0
        assert result.trace.fusion.results == ()
        assert result.trace.rerank.latency_ms == 0
        assert result.trace.rerank.results == ()
    if strategy is RetrievalStrategy.HYBRID:
        assert all(item.rerank_score is None for item in result.evidence)

