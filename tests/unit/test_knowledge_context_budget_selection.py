from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.api.retrieval_projection import project_retrieval_trace
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
from packages.knowledge.retrieval import HybridKnowledgeRetriever


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


class _Reranker:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.received: tuple[str, ...] = ()

    def rerank(self, _query: str, candidates) -> tuple[float, ...]:
        self.received = tuple(candidate.chunk_id for candidate in candidates)
        return tuple(self.scores[candidate.chunk_id] for candidate in candidates)


class _Index:
    def __init__(self, chunk_ids: tuple[str, ...]) -> None:
        self.chunk_ids = chunk_ids

    def dense_search(self, _query, *, scope, limit: int):
        del scope
        return tuple(
            VectorSearchHit(f"point-{index}", 1.0 - index / 100, {"chunk_id": chunk_id})
            for index, chunk_id in enumerate(self.chunk_ids)
        )[:limit]

    def sparse_search(self, _query, *, scope, limit: int):
        return self.dense_search(_query, scope=scope, limit=limit)


def _context(*, permissions=frozenset({"knowledge_run"})) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        permissions=permissions,
    )


def _query(**overrides) -> RetrievalQuery:
    values = {
        "text": "question",
        "knowledge_base_id": str(uuid4()),
        "knowledge_snapshot_id": str(uuid4()),
        "strategy": RetrievalStrategy.HYBRID_RERANK,
        "candidate_top_k": 3,
        "final_top_k": 3,
    }
    return RetrievalQuery(**{**values, **overrides})


def _retriever(
    monkeypatch: pytest.MonkeyPatch,
    *,
    texts: dict[str, str],
    scores: dict[str, float],
    min_rerank_score: float | None = None,
) -> tuple[HybridKnowledgeRetriever, _Reranker]:
    reranker = _Reranker(scores)
    retriever = HybridKnowledgeRetriever(
        session=object(),
        dense_embedder=_Dense(),
        sparse_encoder=_Sparse(),
        reranker=reranker,
        vector_index=_Index(tuple(texts)),
        min_rerank_score=min_rerank_score,
    )

    async def snapshot_scope(_session, _context, _query):
        return uuid4(), uuid4(), (str(uuid4()),)

    async def load_chunks(_session, **_kwargs):
        return {
            chunk_id: (
                SimpleNamespace(
                    chunk_id=chunk_id,
                    text=text,
                    locator={},
                    ordinal=index,
                    normalized_content_hash=f"{index:064d}",
                ),
                SimpleNamespace(
                    id=uuid4(),
                    name=f"{chunk_id}.md",
                    effective_date=None,
                    superseded_by_document_id=None,
                ),
                SimpleNamespace(id=uuid4()),
            )
            for index, (chunk_id, text) in enumerate(texts.items())
        }

    monkeypatch.setattr(retriever, "_snapshot_scope", snapshot_scope)
    monkeypatch.setattr(retriever, "_load_chunks", load_chunks)
    return retriever, reranker


def _json_bytes(records) -> int:
    return len(
        json.dumps(
            records,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


@pytest.mark.asyncio
async def test_budget_uses_actual_utf8_bytes_and_skips_oversized_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(
        monkeypatch,
        texts={"large": "x" * 400, "unicode": "中", "small": "ok"},
        scores={"large": 3.0, "unicode": 2.0, "small": 1.0},
    )
    baseline = await retriever.retrieve_with_trace(_context(), _query())
    unicode_record = asdict(baseline.evidence[1])
    budget = _json_bytes([unicode_record])

    selected = await retriever.retrieve_with_trace(
        _context(),
        _query(context_budget_bytes=budget),
    )

    assert [item.chunk_id for item in selected.evidence] == ["unicode"]
    assert selected.trace.rerank.metadata == {
        "limit": budget,
        "before": 3,
        "after_overlap_dedup": 3,
        "after": 1,
        "bytes": budget,
    }
    assert selected.trace.selection is not None
    assert [item.chunk_id for item in selected.trace.selection.results] == [
        "large",
        "unicode",
        "small",
    ]
    assert reranker.received == ("large", "unicode", "small")
    assert selected.trace.rerank.results[0].score == baseline.trace.rerank.results[0].score


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy",
    [RetrievalStrategy.HYBRID_RERANK, RetrievalStrategy.DENSE],
)
async def test_budget_backfills_beyond_final_top_k_from_ranked_pool(
    strategy: RetrievalStrategy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, _ = _retriever(
        monkeypatch,
        texts={"rank1": "x" * 400, "rank2": "fits"},
        scores={"rank1": 2.0, "rank2": 1.0},
    )
    baseline = await retriever.retrieve_with_trace(
        _context(),
        _query(strategy=strategy, final_top_k=2),
    )
    budget = _json_bytes([asdict(baseline.evidence[1])])

    legacy = await retriever.retrieve_with_trace(
        _context(),
        _query(strategy=strategy, final_top_k=1),
    )
    selected = await retriever.retrieve_with_trace(
        _context(),
        _query(
            strategy=strategy,
            final_top_k=1,
            context_budget_bytes=budget,
        ),
    )

    assert [item.chunk_id for item in legacy.evidence] == ["rank1"]
    assert legacy.trace.rerank.metadata is None
    assert [item.chunk_id for item in selected.evidence] == ["rank2"]
    assert selected.trace.rerank.metadata == {
        "limit": budget,
        "before": 2,
        "after_overlap_dedup": 2,
        "after": 1,
        "bytes": budget,
    }
    if strategy is RetrievalStrategy.HYBRID_RERANK:
        assert [item.chunk_id for item in legacy.trace.rerank.results] == ["rank1"]
        assert [item.chunk_id for item in selected.trace.rerank.results] == [
            "rank1",
            "rank2",
        ]


@pytest.mark.asyncio
async def test_floor_runs_before_budget_and_permission_runs_before_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(
        monkeypatch,
        texts={"high": "x" * 400, "kept": "small"},
        scores={"high": 3.0, "kept": 2.0},
        min_rerank_score=2.5,
    )
    query = _query(context_budget_bytes=2)

    with pytest.raises(AgentHubError) as forbidden:
        await retriever.retrieve_with_trace(_context(permissions=frozenset()), query)
    assert forbidden.value.code == "FORBIDDEN"
    assert reranker.received == ()

    result = await retriever.retrieve_with_trace(_context(), query)

    assert result.trace.dropped_below_floor == 1
    assert result.trace.rerank.metadata == {
        "limit": 2,
        "before": 1,
        "after_overlap_dedup": 1,
        "after": 0,
        "bytes": 2,
    }
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_none_budget_preserves_existing_output_and_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, _ = _retriever(
        monkeypatch,
        texts={"a": "x" * 400, "b": "y" * 400},
        scores={"a": 2.0, "b": 1.0},
    )

    result = await retriever.retrieve_with_trace(_context(), _query(final_top_k=2))

    assert [item.chunk_id for item in result.evidence] == ["a", "b"]
    assert result.trace.rerank.metadata is None


@pytest.mark.asyncio
async def test_playground_projection_exposes_only_safe_budget_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, _ = _retriever(
        monkeypatch,
        texts={"a": "secret evidence"},
        scores={"a": 1.0},
    )

    result = await retriever.retrieve_with_trace(
        _context(),
        _query(final_top_k=1, context_budget_bytes=8_000),
    )
    projection = project_retrieval_trace(result.trace)

    assert projection.rerank.metadata == {
        "limit": 8_000,
        "before": 1,
        "after_overlap_dedup": 1,
        "after": 1,
        "bytes": projection.rerank.metadata["bytes"],
    }
    assert "secret evidence" not in projection.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, 1, 64_001, 1.5, "8000"])
async def test_domain_rejects_invalid_budget_values(
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retriever, reranker = _retriever(
        monkeypatch,
        texts={"a": "evidence"},
        scores={"a": 1.0},
    )

    with pytest.raises(AgentHubError) as error:
        await retriever.retrieve_with_trace(
            _context(),
            _query(context_budget_bytes=value),
        )

    assert error.value.code == "INVALID_RETRIEVAL_QUERY"
    assert reranker.received == ()
