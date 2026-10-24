from dataclasses import replace

import pytest

from benchmarks.public_rag.consumer_replay import (
    CONTEXT,
    CachedConsumerRetriever,
    _Session,
    admit_result,
    compact,
    definition,
    recover_records,
)
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolExecutionContext


def test_historical_preview_recovers_complete_text_records_but_not_refs_or_partial_tail():
    first = {"chunk_id": "a", "snippet": "Actual evidence"}
    content = compact(
        {
            "evidence_refs": [{"chunk_id": "reference_only"}],
            "results": [first, {"chunk_id": "b", "snippet": "partial"}],
        }
    )
    cut = content.index('"partial"') + 4
    payload = {"data": {"preview": content[:cut]}}
    assert recover_records(payload) == [first]
    assert recover_records({"data": {"preview": '{"evidence_refs":[{"chunk_id":"a"}]'}}) == []


@pytest.mark.asyncio
async def test_actual_consumer_path_retains_text_records_and_paired_refs_after_opt_in():
    from uuid import UUID

    units = [
        {
            "document_id": f"4hop1__hidden_label::paragraph::{i}",
            "text": str(i) * 900,
            "title": f"Title {i}",
            "ordinal": i,
        }
        for i in range(5)
    ]
    ranked = [
        {
            "chunk_id": f"chunk-{i}",
            "document_id": u["document_id"],
            "start": 0,
            "end": 900,
            "body": u["text"],
            "rerank_score": float(5 - i),
        }
        for i, u in enumerate(units)
    ]
    retriever = CachedConsumerRetriever(ranked, units)
    configured = definition()
    payload = await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "test"),
        configured,
        {"query": "find supporting sources", "limit": 5},
        _Session,
        retriever=retriever,
    )
    assert "4hop1__" not in compact(payload)
    legacy = replace(
        configured,
        retrieval_config={
            k: v for k, v in configured.retrieval_config.items() if k != "context_budget_bytes"
        },
    )
    _, old_final, _, _ = await admit_result("question", payload, legacy)
    _, new_final, _, usage = await admit_result("question", payload, configured)
    assert len(recover_records(old_final)) < 5
    assert len(recover_records(new_final)) == 5
    data = new_final["data"]
    assert [r["chunk_id"] for r in data["results"]] == [
        r["chunk_id"] for r in data["evidence_refs"]
    ]
    assert len(compact(new_final).encode("utf-8")) <= 8000
    assert usage["estimated_input_after"] <= usage["available_input"]
