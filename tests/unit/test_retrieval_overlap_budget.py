from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from benchmarks.public_rag.consumer_replay import CONTEXT, KB, SNAPSHOT, CachedConsumerRetriever
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.knowledge.evidence_records import EvidenceAdmission, drop_overlapping_records
from packages.knowledge.parser import _parse_content
from packages.knowledge.retrieval import HybridKnowledgeRetriever


def _policy_windows():
    # The OCR block ends in blank lines. Both overlapping fixed windows contain the
    # final sentence, while only the first carries the preceding policy text.
    body = "日常检查记录" * 166 + "检查记录" + "设备复检时间为周四下午三时。" + "\n" * 200
    parsed = _parse_content(
        json.dumps([{"page_idx": 0, "type": "text", "text": body}]).encode(),
        "application/json",
        max_pdf_pages=1,
        max_parsed_chars=2_000,
    )
    return build_deterministic_chunks(
        UUID(int=5000), parsed, chunk_size_chars=1_200, chunk_overlap_chars=200
    )


def _retriever(ranked):
    units = [{"document_id": name, "title": name} for name in ("policy", "notice", "calendar")]
    retriever = CachedConsumerRetriever(ranked, units)
    for source in ranked:
        chunk = retriever.records[source["chunk_id"]][0]
        chunk.locator = source["locator"]
    return retriever


def _source(name, chunk_id, body, locator, score):
    return {
        "document_id": name,
        "chunk_id": chunk_id,
        "body": body,
        "locator": locator,
        "start": locator["char_start"],
        "end": locator["char_end"],
        "rerank_score": score,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", [None, "compact_v1"])
@pytest.mark.parametrize("has_earlier_record", [False, True])
async def test_only_delivered_windows_suppress_later_evidence(projection, has_earlier_record):
    whole, tail = _policy_windows()
    assert tail.text == "设备复检时间为周四下午三时。"
    assert whole.locator["char_end"] == tail.locator["char_end"]
    ranked = [
        _source("policy", whole.chunk_id, whole.text, whole.locator, 3),
        _source("policy", tail.chunk_id, tail.text, tail.locator, 2),
    ]
    calendar = "节假日照常开放。"
    ranked.append(
        _source("calendar", "calendar", calendar, {"char_start": 0, "char_end": len(calendar)}, 1)
    )
    query = RetrievalQuery(
        "设备什么时候复检？",
        knowledge_base_id=str(KB),
        knowledge_snapshot_id=str(SNAPSHOT),
        final_top_k=2,
        evidence_projection=projection,
        context_budget_bytes=64_000,
    )
    # When it fits, the full window suppresses its tail and the next source backfills.
    wide = await _retriever(ranked).retrieve_with_trace(CONTEXT, query)
    assert [e.chunk_id for e in wide.evidence] == [whole.chunk_id, "calendar"]
    assert wide.trace.rerank.metadata["after_overlap_dedup"] == 2

    budget = 2_000
    if has_earlier_record:
        # The large record fits into the total budget on its own but exceeds the
        # remaining budget after the higher-ranked notice is delivered.
        alone = await _retriever(ranked[:1]).retrieve_with_trace(CONTEXT, query)
        budget = alone.trace.rerank.metadata["bytes"] + 20
        notice = "安全通知单独归档。"
        ranked.insert(
            0, _source("notice", "notice", notice, {"char_start": 0, "char_end": len(notice)}, 4)
        )

    result = await _retriever(ranked).retrieve_with_trace(
        CONTEXT, replace(query, context_budget_bytes=budget)
    )
    assert [item.chunk_id for item in result.evidence] == (
        ["notice", tail.chunk_id] if has_earlier_record else [tail.chunk_id, "calendar"]
    )
    delivered = next(item for item in result.evidence if item.chunk_id == tail.chunk_id)
    assert delivered.document_revision_id == str(UUID(int=5000))
    assert delivered.text == tail.text
    assert delivered.locator == tail.locator
    assert result.trace.rerank.metadata["before"] == len(ranked)
    assert result.trace.rerank.metadata["after_overlap_dedup"] == len(ranked)
    assert result.trace.rerank.metadata["bytes"] <= budget


@pytest.mark.parametrize(
    "first,second",
    [
        (
            {"paragraph_ordinal": 3, "char_start": 800, "char_end": 850},
            {"paragraph_ordinal": 3, "start": 810, "end": 825},
        ),
        (
            {"type": "line_range", "start": 8, "end": 15},
            {"type": "paragraph_index", "index": 2, "start": 9, "end": 11},
        ),
    ],
)
def test_containment_compares_positions_in_the_same_coordinate_system(first, second):
    records = [
        {"chunk_id": str(i), "document_revision_id": "manual", "locator": locator}
        for i, locator in enumerate((first, second))
    ]
    references = [{"chunk_id": record["chunk_id"]} for record in records]
    assert drop_overlapping_records(records, references) == (records, references)


@pytest.mark.asyncio
async def test_consumer_text_budget_can_admit_a_short_window_and_backfill():
    from packages.agent_runtime.context_budget import ApproxTokenEstimator
    from packages.knowledge.evidence_records import EvidenceTextBudget

    whole, tail = _policy_windows()
    calendar = "节假日照常开放。"
    ranked = [
        _source("policy", whole.chunk_id, whole.text, whole.locator, 3),
        _source("policy", tail.chunk_id, tail.text, tail.locator, 2),
        _source("calendar", "calendar", calendar, {"char_start": 0, "char_end": len(calendar)}, 1),
    ]
    query = RetrievalQuery(
        "设备什么时候复检？", str(KB), str(SNAPSHOT), candidate_top_k=3, final_top_k=2
    )
    estimate = ApproxTokenEstimator().estimate
    retriever = _retriever(ranked)
    result = await HybridKnowledgeRetriever.retrieve_with_trace(
        retriever,
        CONTEXT,
        query,
        text_budget=EvidenceTextBudget(estimate(tail.text) + estimate(calendar), estimate),
    )
    assert [item.chunk_id for item in result.evidence] == [tail.chunk_id, "calendar"]
    assert result.evidence[0].locator == tail.locator


@pytest.mark.asyncio
async def test_dense_candidate_limit_counts_distinct_hydratable_chunk_ids():
    first = _source("notice", "notice", "Office closed.", {"char_start": 0, "char_end": 14}, 3)
    second = _source("calendar", "calendar", "Open Friday.", {"char_start": 0, "char_end": 12}, 1)
    retriever = _retriever([first, first, second])
    result = await retriever.retrieve_with_trace(
        CONTEXT,
        RetrievalQuery(
            "When is the office open?",
            str(KB),
            str(SNAPSHOT),
            strategy=RetrievalStrategy.DENSE,
            candidate_top_k=2,
            final_top_k=2,
        ),
    )
    assert [(item.chunk_id, item.retrieval_score) for item in result.evidence] == [
        ("notice", 1.0),
        ("calendar", pytest.approx(1 / 3)),
    ]


def test_covered_windows_do_not_inflate_citation_ids_at_an_exact_byte_boundary():
    records = [
        {
            "chunk_id": str(i),
            "document_revision_id": "policy",
            "snippet": text,
            "locator": {"char_start": start, "char_end": end},
        }
        for i, (text, start, end) in enumerate(
            [("0123456789", 0, 10), *((str(i), i, i + 1) for i in range(8)), ("X", 15, 16)]
        )
    ]
    refs = [
        dict(
            snapshot_id="snapshot",
            knowledge_base_id="kb",
            document_revision_id="policy",
            chunk_id=record["chunk_id"],
        )
        for record in records
    ]
    options = dict(estimator=lambda s: len(s.encode()), max_items=2)
    expected = assemble_evidence_plan(records[::9], refs[::9], budget=64_000, **options)
    budget = len(json.dumps(expected.payload, separators=(",", ":")).encode())
    actual = assemble_evidence_plan(
        records, refs, budget=budget, evidence_admission=EvidenceAdmission(), **options
    )
    assert actual == expected
