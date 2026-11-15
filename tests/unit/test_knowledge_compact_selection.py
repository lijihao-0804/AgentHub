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
    # Retrieval stores 1,300 source characters; the source DTO keeps identity
    # metadata off the model's reading budget.
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
        assert record["snippet"] == source["body"]
        assert record["snapshot_id"] == reference["snapshot_id"] == str(SNAPSHOT)
        assert record["chunk_id"] == reference["chunk_id"] == source["chunk_id"]


@pytest.mark.asyncio
async def test_published_search_restores_cross_snapshot_slot_and_long_exception():
    from benchmarks.public_rag.consumer_replay import recover_records
    from benchmarks.public_rag.evidence_plan_replay import admit

    exception = " Refunds exclude clearance stock."
    long_chunk = "A" * 2100 + exception
    units = [{"document_id": str(i), "title": f"Policy {i}"} for i in range(2)]
    ranked = [
        {
            "document_id": str(i),
            "chunk_id": str(UUID(int=30 + i)),
            "body": body,
            "start": 0,
            "end": len(body),
            "rerank_score": 2 - i,
        }
        for i, body in enumerate([long_chunk, "Alternative proof is accepted."])
    ]
    retriever = CachedConsumerRetriever(ranked, units)
    tool = definition()
    tool = replace(tool, snapshot_refs=(*tool.snapshot_refs, *tool.snapshot_refs))
    payload = await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "consumer-retrieval"),
        tool,
        {"query": "refunds", "limit": 2},
        _Session,
        retriever=retriever,
    )
    _, admitted, *_ = await admit("refunds", payload, tool)
    assert [r["snippet"] for r in recover_records(admitted)] == [r["body"] for r in ranked]
    assert [r["chunk_id"] for r in payload["evidence_refs"]] == [r["chunk_id"] for r in ranked]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_published_sentence_option_reaches_tool_and_falls_back_to_valid_whole_scores(failed):
    from packages.agent_runtime.publish import _validate_retrieval_config
    from packages.knowledge.contracts import KnowledgeProviderError

    units = [{"document_id": str(i), "title": str(i)} for i in range(2)]
    ranked = [
        {
            "document_id": str(i),
            "chunk_id": str(UUID(int=33 + i)),
            "body": s,
            "start": 0,
            "end": len(s),
            "rerank_score": 2 - i,
        }
        for i, s in enumerate(["Generic context.", "Only a signed receipt is needed."])
    ]
    retriever = CachedConsumerRetriever(ranked, units)
    baseline = retriever.reranker

    class Scorer:
        def rerank(self, query, items):
            if items and items[0].chunk_id in baseline.scores:
                return baseline.rerank(query, items)
            if failed:
                raise KnowledgeProviderError("LOCAL_INFERENCE_FAILED", "test failure")
            return tuple(8.0 if "receipt" in c.text else -8.0 for c in items)

    retriever.reranker = Scorer()
    config = _validate_retrieval_config({"rerank_method": "sentence_evidence_v1"})
    assert "rerank_method" not in _validate_retrieval_config({})
    tool = definition()
    tool = replace(tool, retrieval_config={**tool.retrieval_config, **config})
    payload = await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "consumer-retrieval"),
        tool,
        {"query": "refunds", "limit": 1},
        _Session,
        retriever=retriever,
    )
    assert payload["results"][0]["snippet"] == ranked[0 if failed else 1]["body"]
    stage = retriever.last.trace.rerank.metadata
    assert stage["rerank_method"] == "sentence_evidence_v1"
    assert (stage.get("rerank_fallback") == "LOCAL_INFERENCE_FAILED") is failed
