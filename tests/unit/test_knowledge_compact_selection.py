from dataclasses import replace
from uuid import UUID

import pytest

from benchmarks.public_rag.consumer_replay import (
    CONTEXT,
    KB,
    SNAPSHOT,
    CachedConsumerRetriever,
    _Session,
    definition,
)
from packages.knowledge.contracts import RetrievalQuery
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolExecutionContext


@pytest.mark.asyncio
async def test_selection_budgets_reading_view_and_preserves_rank_and_excerpt() -> None:
    # Retrieval stores 1,300 source characters, while the builtin can expose
    # only 1,000. The source DTO must not consume the model's reading budget.
    units = [{"document_id": f"unit-{i}", "title": f"Title {i}"} for i in range(7)]
    ranked = [
        {
            "document_id": unit["document_id"],
            "chunk_id": str(UUID(int=100 + i)),
            "body": f"Passage {i}: " + "x" * 1_300,
            "start": 0,
            "end": 1_311,
            "rerank_score": 1 - i * 0.1,
        }
        for i, unit in enumerate(units)
    ]
    retriever = CachedConsumerRetriever(ranked, units)
    query = RetrievalQuery(
        text="question",
        knowledge_base_id=str(KB),
        knowledge_snapshot_id=str(SNAPSHOT),
        dense_top_k=7,
        sparse_top_k=7,
        candidate_top_k=7,
        final_top_k=5,
        context_budget_bytes=8_000,
    )
    legacy = await retriever.retrieve_with_trace(CONTEXT, query)
    compact = await retriever.retrieve_with_trace(
        CONTEXT, replace(query, evidence_projection="compact_v1")
    )
    assert len(legacy.evidence) < 5
    assert [item.chunk_id for item in compact.evidence] == [r["chunk_id"] for r in ranked[:5]]
    assert compact.trace.rerank.results == legacy.trace.rerank.results

    tool = definition()
    tool = replace(
        tool, retrieval_config={**tool.retrieval_config, "evidence_projection": "compact_v1"}
    )
    payload = await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "provider-id"),
        tool,
        {"query": "question", "limit": 5},
        _Session,
        retriever=retriever,
    )
    assert len(payload["results"]) == 5
    for record, reference, source in zip(
        payload["results"], payload["evidence_refs"], ranked[:5], strict=True
    ):
        assert record["snippet"] == source["body"][:1_000]
        assert record["snapshot_id"] == reference["snapshot_id"] == str(SNAPSHOT)
        assert record["chunk_id"] == reference["chunk_id"] == source["chunk_id"]
