from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.public_rag.consumer_replay import CONTEXT, KB, SNAPSHOT
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceStage,
)
from packages.knowledge.evidence_set import EvidenceSetPlan
from packages.knowledge.evidence_set_retriever import EvidenceSetKnowledgeRetriever
from tests.unit.test_complementary_evidence_set import sources


@pytest.mark.asyncio
async def test_composed_retriever_selects_original_sources_after_scoped_floor_before_budget():
    evidence = sources()
    empty = RetrievalTraceStage(0, ())
    trace = RetrievalTrace(str(SNAPSHOT), empty, empty, empty, empty, 5, dropped_below_floor=2)
    base = SimpleNamespace(
        retrieve_with_trace=AsyncMock(return_value=RetrievalResult(evidence, trace))
    )
    selector = SimpleNamespace(
        select=AsyncMock(
            return_value=EvidenceSetPlan(
                (evidence[0], evidence[1]), (evidence[1].chunk_id, evidence[0].chunk_id)
            )
        )
    )
    reader = EvidenceSetKnowledgeRetriever(base, selector)
    query = RetrievalQuery(
        "question",
        str(KB),
        str(SNAPSHOT),
        final_top_k=2,
        min_rerank_score=0.2,
        context_budget_bytes=8000,
    )
    result = await reader.retrieve_with_trace(CONTEXT, query)
    captured = base.retrieve_with_trace.call_args.args[1]
    assert captured.knowledge_snapshot_id == query.knowledge_snapshot_id
    assert captured.min_rerank_score == 0.2 and captured.final_top_k == 20
    assert captured.context_budget_bytes is None
    assert result.evidence == evidence[:2] and result.evidence[0] is evidence[0]
    assert result.trace.dropped_below_floor == 2
    assert result.trace.evidence_set.metadata["candidate_count"] == 3
    selector.select.assert_awaited_once()
    selector.select.return_value = EvidenceSetPlan((replace(evidence[0], text="invented"),), ())
    with pytest.raises(ValueError):
        await reader.retrieve_with_trace(CONTEXT, query)


@pytest.mark.asyncio
async def test_composed_retriever_refuses_unbounded_or_other_projection_before_io():
    base = SimpleNamespace(retrieve_with_trace=AsyncMock())
    reader = EvidenceSetKnowledgeRetriever(base, SimpleNamespace())
    with pytest.raises(ValueError):
        await reader.retrieve_with_trace(CONTEXT, RetrievalQuery("q", str(KB), str(SNAPSHOT)))
    base.retrieve_with_trace.assert_not_awaited()
