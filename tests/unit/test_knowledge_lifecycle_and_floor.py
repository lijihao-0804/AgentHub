"""Relevance floor and cross-document supersession.

Both behaviours exist because of one measured failure on a 60-document corpus:
retrieval always returned its ``final_top_k`` best chunks, so a question the
corpus could not answer produced six irrelevant ones, and a retired policy
out-ranked the document that replaced it in 4 of 14 version-sensitive cases.
"""

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

_CHUNKS = ("c1", "c2", "c3")


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


class _ScriptedReranker:
    """Returns a fixed score per chunk so ranking is exactly predictable."""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    def rerank(self, _query: str, candidates) -> tuple[float, ...]:
        return tuple(self.scores[candidate.chunk_id] for candidate in candidates)


class _Index:
    def dense_search(self, _query, *, scope, limit: int):
        return tuple(
            VectorSearchHit(f"p{index}", 1.0 - index / 10, {"chunk_id": chunk_id})
            for index, chunk_id in enumerate(_CHUNKS)
        )[:limit]

    def sparse_search(self, _query, *, scope, limit: int):
        return self.dense_search(_query, scope=scope, limit=limit)


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


def _query(**overrides) -> RetrievalQuery:
    base = {
        "text": "what is the password policy",
        "knowledge_base_id": str(uuid4()),
        "knowledge_snapshot_id": str(uuid4()),
        "strategy": RetrievalStrategy.HYBRID_RERANK,
        "final_top_k": 3,
    }
    return RetrievalQuery(**{**base, **overrides})


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scores: dict[str, float],
    documents: dict[str, SimpleNamespace] | None = None,
    **retriever_kwargs,
) -> HybridKnowledgeRetriever:
    retriever = HybridKnowledgeRetriever(
        session=object(),
        dense_embedder=_Dense(),
        sparse_encoder=_Sparse(),
        reranker=_ScriptedReranker(scores),
        vector_index=_Index(),
        **retriever_kwargs,
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
                (documents or {}).get(
                    chunk_id,
                    SimpleNamespace(
                        id=uuid4(),
                        name=f"{chunk_id}.md",
                        effective_date=None,
                        superseded_by_document_id=None,
                    ),
                ),
                SimpleNamespace(id=uuid4()),
            )
            for chunk_id in _CHUNKS
        }

    monkeypatch.setattr(retriever, "_snapshot_scope", snapshot_scope)
    monkeypatch.setattr(retriever, "_load_chunks", load_chunks)
    return retriever


@pytest.mark.asyncio
async def test_the_floor_drops_weak_evidence_and_says_how_much_it_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = _build(
        monkeypatch,
        scores={"c1": 3.0, "c2": 1.0, "c3": 0.5},
        min_rerank_score=1.5,
    )

    result = await retriever.retrieve_with_trace(_context(), _query())

    assert [item.chunk_id for item in result.evidence] == ["c1"]
    assert result.trace.dropped_below_floor == 2
    # The trace still shows the dropped chunks and their scores; that is the
    # only way to calibrate the floor from a real query.
    assert len(result.trace.rerank.results) == 3


@pytest.mark.asyncio
async def test_an_unanswerable_query_returns_nothing_rather_than_the_least_bad_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = _build(
        monkeypatch,
        scores={"c1": -5.0, "c2": -6.0, "c3": -7.0},
        min_rerank_score=1.5,
    )

    result = await retriever.retrieve_with_trace(_context(), _query())

    assert result.evidence == ()
    assert result.trace.dropped_below_floor == 3


@pytest.mark.asyncio
async def test_a_query_may_override_the_configured_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = _build(
        monkeypatch,
        scores={"c1": 3.0, "c2": 1.0, "c3": 0.5},
        min_rerank_score=1.5,
    )

    result = await retriever.retrieve_with_trace(
        _context(), _query(min_rerank_score=-100.0)
    )

    assert len(result.evidence) == 3
    assert result.trace.dropped_below_floor == 0


@pytest.mark.asyncio
async def test_no_configured_floor_keeps_every_reranked_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever = _build(monkeypatch, scores={"c1": -5.0, "c2": -6.0, "c3": -7.0})

    result = await retriever.retrieve_with_trace(_context(), _query())

    assert len(result.evidence) == 3
    assert result.trace.dropped_below_floor == 0


@pytest.mark.asyncio
async def test_the_floor_is_not_applied_where_there_is_no_rerank_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An RRF fusion score is a rank statistic on an unrelated scale, so a floor
    # calibrated against rerank scores would silently empty every HYBRID query.
    retriever = _build(
        monkeypatch,
        scores={"c1": 3.0, "c2": 1.0, "c3": 0.5},
        min_rerank_score=1.5,
    )

    result = await retriever.retrieve_with_trace(
        _context(), _query(strategy=RetrievalStrategy.HYBRID)
    )

    assert len(result.evidence) == 3
    assert all(item.rerank_score is None for item in result.evidence)
    assert result.trace.dropped_below_floor == 0


def _superseded_document(successor_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name="password-policy-v1.md",
        effective_date=None,
        superseded_by_document_id=successor_id,
    )


@pytest.mark.asyncio
async def test_a_superseded_document_loses_a_slot_it_would_otherwise_have_won(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    successor_id = uuid4()
    documents = {"c1": _superseded_document(successor_id)}
    scores = {"c1": 3.0, "c2": 2.0, "c3": 0.0}

    without_penalty = _build(monkeypatch, scores=scores, documents=documents)
    baseline = await without_penalty.retrieve_with_trace(_context(), _query(final_top_k=1))
    assert [item.chunk_id for item in baseline.evidence] == ["c1"]

    with_penalty = _build(
        monkeypatch,
        scores=scores,
        documents=documents,
        superseded_rank_penalty=2.0,
    )
    penalised = await with_penalty.retrieve_with_trace(_context(), _query(final_top_k=1))

    # The penalty is applied before the cut, so it changes *which* chunk wins
    # the single slot -- not merely the order of chunks that already won.
    assert [item.chunk_id for item in penalised.evidence] == ["c2"]


@pytest.mark.asyncio
async def test_a_superseded_document_is_still_reachable_when_nothing_else_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "What did the old policy say" is a legitimate question: supersession is a
    # penalty, never a filter.
    documents = {"c1": _superseded_document(uuid4())}
    retriever = _build(
        monkeypatch,
        scores={"c1": 9.0, "c2": 1.0, "c3": 0.0},
        documents=documents,
        superseded_rank_penalty=2.0,
    )

    result = await retriever.retrieve_with_trace(_context(), _query(final_top_k=1))

    assert [item.chunk_id for item in result.evidence] == ["c1"]
    assert result.evidence[0].rerank_score == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_evidence_carries_the_lifecycle_the_model_needs_to_see(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    successor_id = uuid4()
    live = SimpleNamespace(
        id=uuid4(),
        name="password-policy-v2.md",
        effective_date=date(2026, 1, 1),
        superseded_by_document_id=None,
    )
    documents = {"c1": _superseded_document(successor_id), "c2": live}
    retriever = _build(
        monkeypatch,
        scores={"c1": 3.0, "c2": 2.0, "c3": 1.0},
        documents=documents,
    )

    result = await retriever.retrieve_with_trace(_context(), _query())
    by_chunk = {item.chunk_id: item.metadata for item in result.evidence}

    assert by_chunk["c1"]["document_name"] == "password-policy-v1.md"
    assert by_chunk["c1"]["superseded"] is True
    assert by_chunk["c1"]["superseded_by_document_id"] == str(successor_id)
    assert by_chunk["c1"]["effective_date"] is None
    assert by_chunk["c2"]["superseded"] is False
    assert by_chunk["c2"]["superseded_by_document_id"] is None
    # Serialised, not a date object: this metadata is handed to a tool result.
    assert by_chunk["c2"]["effective_date"] == "2026-01-01"


def test_a_negative_supersession_penalty_is_refused() -> None:
    # A negative penalty would *promote* retired documents, which is the exact
    # failure this setting exists to prevent.
    with pytest.raises(ValueError):
        HybridKnowledgeRetriever(
            session=object(),
            dense_embedder=_Dense(),
            sparse_encoder=_Sparse(),
            reranker=_ScriptedReranker({}),
            vector_index=_Index(),
            superseded_rank_penalty=-1.0,
        )
