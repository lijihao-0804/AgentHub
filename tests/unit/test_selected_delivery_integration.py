"""Regression boundaries for the independently selected PR #19 delivery fixes."""

from dataclasses import replace
from uuid import UUID

import pytest

from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.evidence_records import (
    drop_overlapping_evidence,
    drop_overlapping_records,
    evidence_record,
)


def evidence(chunk, locator, text="source text"):
    return RetrievedEvidence(
        document_id=str(UUID(int=1)),
        document_revision_id=str(UUID(int=2)),
        chunk_id=chunk,
        source="synthetic test source",
        locator=locator,
        text=text,
        retrieval_score=1.0,
        rerank_score=None,
        metadata={},
    )


def test_long_record_keeps_tail_exception_before_whole_record_budgeting():
    body = "A" * 2100 + " Refunds exclude clearance stock."
    item = evidence("long", {"char_start": 0, "char_end": len(body)}, body)
    record = evidence_record(item, snapshot_id="snapshot")
    assert record["snippet"] == body
    assert record["snapshot_id"] == "snapshot"


@pytest.mark.parametrize("scope", ["page", "paragraph_ordinal"])
def test_local_offsets_in_distinct_source_blocks_do_not_suppress_text(scope):
    items = [evidence(str(i), {scope: i, "char_start": 0, "char_end": 50}) for i in (1, 2)]
    assert drop_overlapping_evidence(items) == tuple(items)


def test_unknown_coordinate_scope_is_not_treated_as_comparable():
    items = [evidence(str(i), {"page": True, "char_start": 0, "char_end": 50}) for i in (1, 2)]
    assert drop_overlapping_evidence(items) == tuple(items)


def test_coordinate_free_duplicate_keeps_its_original_reference_pair():
    record = evidence_record(evidence("same", {"page": 1}))
    refs = [{"snapshot_id": "first"}, {"snapshot_id": "second"}]
    records, kept_refs = drop_overlapping_records([record, dict(record)], refs)
    assert records == [record]
    assert kept_refs == [refs[0]]


def test_partial_overlap_survives_but_same_scope_containment_does_not():
    items = [
        evidence("a", {"page": 1, "char_start": 0, "char_end": 1200}),
        evidence("b", {"page": 1, "char_start": 1000, "char_end": 2200}),
        evidence("c", {"page": 1, "char_start": 1100, "char_end": 1150}),
    ]
    assert drop_overlapping_evidence(items) == tuple(items[:2])


@pytest.mark.asyncio
async def test_multi_snapshot_tool_delivery_preserves_tail_and_complementary_slot():
    from benchmarks.public_rag.consumer_replay import (
        CONTEXT,
        CachedConsumerRetriever,
        _Session,
        definition,
        recover_records,
    )
    from benchmarks.public_rag.evidence_plan_replay import admit
    from packages.tools.builtins.search_knowledge import search_knowledge
    from packages.tools.contracts import ToolExecutionContext

    bodies = ["A" * 2100 + " Refunds exclude clearance stock.", "Alternative proof is accepted."]
    units = [{"document_id": str(i), "title": f"Policy {i}"} for i in range(2)]
    ranked = [
        dict(
            document_id=str(i),
            chunk_id=str(UUID(int=30 + i)),
            body=body,
            start=0,
            end=len(body),
            rerank_score=2 - i,
        )
        for i, body in enumerate(bodies)
    ]
    retriever = CachedConsumerRetriever(ranked, units)
    tool = definition()
    tool = replace(tool, snapshot_refs=(*tool.snapshot_refs, *tool.snapshot_refs))
    payload = await search_knowledge(
        ToolExecutionContext(CONTEXT, UUID(int=9), "delivery-regression"),
        tool,
        {"query": "refunds", "limit": 2},
        _Session,
        retriever=retriever,
    )
    assert [r["snippet"] for r in payload["results"]] == bodies
    assert [r["chunk_id"] for r in payload["evidence_refs"]] == [r["chunk_id"] for r in ranked]
    _, admitted, *_ = await admit("refunds", payload, tool)
    assert [r["snippet"] for r in recover_records(admitted)] == bodies
