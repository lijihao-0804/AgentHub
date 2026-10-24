from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
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
from packages.knowledge.retrieval import (
    HybridKnowledgeRetriever,
    _FusedCandidate,
    select_rerank_candidates,
)

_DOCUMENT_IDS = {f"c{index}": uuid4() for index in range(1, 6)}


class _Dense:
    def embed_query(self, _text: str) -> tuple[float, ...]:
        return (1.0,)

    def embed_documents(self, _texts):
        return ()

    @property
    def dimension(self) -> int:
        return 1


class _Sparse:
    def encode_query(self, _text: str) -> SparseEncoding:
        return SparseEncoding((1,), (1.0,))

    def encode_documents(self, _texts):
        return ()


class _RecordingReranker:
    def __init__(self) -> None:
        self.received: tuple[str, ...] = ()

    def rerank(self, _query: str, candidates) -> tuple[float, ...]:
        self.received = tuple(candidate.chunk_id for candidate in candidates)
        return tuple(1.0 for _candidate in candidates)


class _RecordingSpan:
    def __init__(self, sink: _RecordingTraceSink, name: str) -> None:
        self.sink = sink
        self.name = name

    async def end(self, *, attributes=None, status: str = "ok", failure_code=None) -> None:
        self.sink.ended.append((self.name, dict(attributes or {}), status, failure_code))


class _RecordingTraceSink:
    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, object]]] = []
        self.ended: list[tuple[str, dict[str, object], str, str | None]] = []

    async def start_span(self, name: str, attributes=None) -> _RecordingSpan:
        self.started.append((name, dict(attributes or {})))
        return _RecordingSpan(self, name)


class _Index:
    def dense_search(self, _query, *, scope, limit: int):
        return (
            VectorSearchHit("p1", 0.9, {"chunk_id": "c1"}),
            VectorSearchHit("p2", 0.8, {"chunk_id": "c2"}),
            VectorSearchHit("p3", 0.7, {"chunk_id": "c3"}),
            VectorSearchHit("p4", 0.6, {"chunk_id": "c4"}),
            VectorSearchHit("p5", 0.5, {"chunk_id": "c5"}),
        )[:limit]

    def sparse_search(self, _query, *, scope, limit: int):
        return self.dense_search(_query, scope=scope, limit=limit)


def _candidate(chunk_id: str, score: float, rank: int) -> _FusedCandidate:
    return _FusedCandidate(chunk_id=chunk_id, retrieval_score=score, best_source_rank=rank)


def test_budget_and_quota_are_independent_while_deferred_chunks_runtime_fill() -> None:
    candidates = (
        _candidate("a1", 0.9, 1),
        _candidate("a2", 0.8, 2),
        _candidate("a3", 0.7, 3),
        _candidate("b1", 0.6, 4),
        _candidate("c1", 0.5, 5),
    )

    selected = select_rerank_candidates(
        candidates,
        document_ids={
            "a1": "doc-a",
            "a2": "doc-a",
            "a3": "doc-a",
            "b1": "doc-b",
            "c1": "doc-c",
        },
        limit=4,
        max_chunks_per_document=1,
    )

    assert [item.chunk_id for item in selected] == ["a1", "a2", "b1", "c1"]


def test_a_deferred_chunk_can_restore_multiple_evidence_from_one_document() -> None:
    candidates = (
        _candidate("a1", 0.99, 1),
        _candidate("a2", 0.98, 2),
        _candidate("a3", 0.40, 3),
        _candidate("a4", 0.30, 4),
        _candidate("a5", 0.20, 5),
    )

    selected = select_rerank_candidates(
        candidates,
        document_ids={item.chunk_id: "doc-a" for item in candidates},
        limit=3,
        max_chunks_per_document=1,
    )

    assert [item.chunk_id for item in selected] == ["a1", "a2", "a3"]


def test_missing_database_rows_neither_occupy_budget_nor_document_quota() -> None:
    candidates = (
        _candidate("missing-a", 0.99, 1),
        _candidate("a1", 0.90, 2),
        _candidate("missing-b", 0.80, 3),
        _candidate("a2", 0.70, 4),
        _candidate("b1", 0.60, 5),
    )

    selected = select_rerank_candidates(
        candidates,
        document_ids={"a1": "doc-a", "a2": "doc-a", "b1": "doc-b"},
        limit=3,
        max_chunks_per_document=1,
    )

    assert [item.chunk_id for item in selected] == ["a1", "a2", "b1"]


def test_selection_preserves_fusion_order_despite_budget_and_quota() -> None:
    candidates = tuple(_candidate(f"c{index}", 1.0 - index / 100, index) for index in range(1, 7))

    selected = select_rerank_candidates(
        candidates,
        document_ids={
            "c1": "doc-a",
            "c2": "doc-a",
            "c3": "doc-b",
            "c4": "doc-b",
            "c5": "doc-c",
            "c6": "doc-c",
        },
        limit=4,
        max_chunks_per_document=1,
    )

    assert [item.chunk_id for item in selected] == ["c1", "c2", "c3", "c5"]
    assert selected == tuple(item for item in candidates if item in selected)


@pytest.mark.parametrize(
    ("limit", "max_chunks_per_document"),
    [(0, None), (-1, None), (1, 0), (1, -1)],
)
def test_selector_rejects_non_positive_budgets(
    limit: int,
    max_chunks_per_document: int | None,
) -> None:
    with pytest.raises(ValueError):
        select_rerank_candidates(
            (_candidate("c1", 1.0, 1),),
            document_ids={"c1": "doc-a"},
            limit=limit,
            max_chunks_per_document=max_chunks_per_document,
        )


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


def _query(
    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID_RERANK,
    **overrides,
) -> RetrievalQuery:
    values = {
        "text": "question",
        "knowledge_base_id": str(uuid4()),
        "knowledge_snapshot_id": str(uuid4()),
        "strategy": strategy,
        "candidate_top_k": 5,
        "final_top_k": 5,
    }
    return RetrievalQuery(**{**values, **overrides})


def _retriever(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trace_sink: _RecordingTraceSink | None = None,
) -> tuple[HybridKnowledgeRetriever, _RecordingReranker]:
    reranker = _RecordingReranker()
    retriever = HybridKnowledgeRetriever(
        session=object(),
        dense_embedder=_Dense(),
        sparse_encoder=_Sparse(),
        reranker=reranker,
        vector_index=_Index(),
        trace_sink=trace_sink,
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
                    id=document_id,
                    name=f"{chunk_id}.md",
                    effective_date=None,
                    superseded_by_document_id=None,
                ),
                SimpleNamespace(id=uuid4()),
            )
            for chunk_id, document_id in _DOCUMENT_IDS.items()
        }

    monkeypatch.setattr(retriever, "_snapshot_scope", snapshot_scope)
    monkeypatch.setattr(retriever, "_load_chunks", load_chunks)
    return retriever, reranker


@pytest.mark.asyncio
async def test_hybrid_rerank_uses_separate_budget_and_traces_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_sink = _RecordingTraceSink()
    retriever, reranker = _retriever(monkeypatch, trace_sink=trace_sink)

    result = await retriever.retrieve_with_trace(
        _context(),
        _query(rerank_top_k=2, final_top_k=5),
    )

    assert reranker.received == ("c1", "c2")
    assert [item.chunk_id for item in result.evidence] == ["c1", "c2"]
    assert result.trace.selection is not None
    assert [item.chunk_id for item in result.trace.selection.results] == ["c1", "c2"]
    assert result.trace.selection.results[0].score == result.trace.fusion.results[0].score
    assert len(result.trace.rerank.results) == 2
    rerank_start = next(
        attributes for name, attributes in trace_sink.started if name == "knowledge.rerank"
    )
    rerank_end = next(
        attributes
        for name, attributes, _status, _failure_code in trace_sink.ended
        if name == "knowledge.rerank"
    )
    assert rerank_start["candidate_count"] == 5
    assert rerank_start["rerank_input_count"] == 2
    assert rerank_end["candidate_count"] == 5
    assert rerank_end["rerank_input_count"] == 2


@pytest.mark.asyncio
async def test_hybrid_rerank_respects_soft_document_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(monkeypatch)
    document_ids = {
        "c1": uuid4(),
        "c3": uuid4(),
        "c5": uuid4(),
    }
    document_ids["c2"] = document_ids["c1"]
    document_ids["c4"] = document_ids["c3"]

    async def load_shared_documents(_session, **_kwargs):
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
                    id=document_id,
                    name=f"{document_id}.md",
                    effective_date=None,
                    superseded_by_document_id=None,
                ),
                SimpleNamespace(id=uuid4()),
            )
            for chunk_id, document_id in document_ids.items()
        }

    monkeypatch.setattr(retriever, "_load_chunks", load_shared_documents)

    result = await retriever.retrieve_with_trace(
        _context(),
        _query(rerank_top_k=4, final_top_k=4, max_chunks_per_document=1),
    )

    assert reranker.received == ("c1", "c2", "c3", "c5")
    assert [item.chunk_id for item in result.evidence] == ["c1", "c2", "c3", "c5"]
    assert result.trace.selection is not None
    assert [item.chunk_id for item in result.trace.selection.results] == [
        "c1",
        "c2",
        "c3",
        "c5",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy",
    [RetrievalStrategy.DENSE, RetrievalStrategy.HYBRID],
)
async def test_other_strategies_ignore_rerank_budget_and_selection(
    strategy: RetrievalStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(monkeypatch)

    result = await retriever.retrieve_with_trace(
        _context(),
        _query(strategy=strategy, rerank_top_k=1, final_top_k=5),
    )

    assert reranker.received == ()
    assert len(result.evidence) == 5
    assert result.trace.selection is None


@pytest.mark.asyncio
async def test_default_hybrid_rerank_path_still_uses_the_whole_hydrated_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(monkeypatch)

    result = await retriever.retrieve_with_trace(_context(), _query())

    assert reranker.received == ("c1", "c2", "c3", "c4", "c5")
    assert len(result.evidence) == 5
    assert result.trace.selection is not None
    assert len(result.trace.selection.results) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"rerank_top_k": 0},
        {"rerank_top_k": -1},
        {"max_chunks_per_document": 0},
        {"max_chunks_per_document": -1},
        {"candidate_top_k": 2, "rerank_top_k": 3},
    ],
)
async def test_retrieval_rejects_invalid_optional_budgets(
    overrides: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(monkeypatch)

    with pytest.raises(AgentHubError) as error:
        await retriever.retrieve_with_trace(_context(), _query(**overrides))

    assert error.value.code == "INVALID_RETRIEVAL_QUERY"
    assert reranker.received == ()
