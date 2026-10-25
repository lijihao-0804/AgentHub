"""Regression cases for routing requirements independent of retrieval algorithms."""

from uuid import UUID

import pytest

from packages.wiki.query import _find_conflicts, query_wiki
from tests.unit.test_wiki_query import (
    _chunk,
    _page,
    _setup,
    _StubSession,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["fact", "synthesis"])
async def test_auto_fact_and_synthesis_cannot_skip_raw_on_wiki_hit(intent):
    page = _page()
    context, kb_id, compilation_id, snapshot_id, service = _setup(pages=[page])
    claim = page["claims"][0]
    chunk = _chunk(
        document_id=UUID(claim["document_id"]), revision_id=UUID(claim["document_revision_id"])
    )
    result = await query_wiki(
        _StubSession([chunk]),
        context,
        kb_id,
        compilation_id,
        snapshot_id,
        "retention policy",
        intent=intent,
        service=service,
    )
    assert result["supplemental_required"] is True
    assert result["insufficient_evidence"] is True


def test_conflicts_are_scoped_to_topic_and_single_marked_claim_survives():
    evidence = [
        {
            "source": "wiki_claim",
            "topic_key": "retention",
            "predicate": "period",
            "condition": "",
            "value": "7y",
            "conflict": True,
            "chunk_id": "a",
            "quote": "a",
        },
        {
            "source": "wiki_claim",
            "topic_key": "backup",
            "predicate": "period",
            "condition": "",
            "value": "1d",
            "conflict": False,
            "chunk_id": "b",
            "quote": "b",
        },
    ]
    found = _find_conflicts(evidence)
    assert [c["chunk_id"] for c in found] == ["a"]
    assert evidence[1]["conflict"] is False


@pytest.mark.asyncio
async def test_related_topic_retrieves_both_conflict_sources_before_answering():
    records = _page(title="Records", chunk_id="record", quote="Records need review.")
    first = _page(title="Policy alpha", chunk_id="alpha", quote="Keep for 7 years.", conflict=True)
    second = _page(title="Archive beta", chunk_id="beta", quote="Keep for 5 years.", conflict=True)
    records["relations"] = [first["page_key"]]
    for page in (first, second):
        page["claims"][0]["conflict_peers"] = ["alpha", "beta"]
    context, kb, compilation, snapshot, service = _setup(pages=[records, first, second])
    chunks = [
        _chunk(
            chunk_id=p["claims"][0]["chunk_id"],
            text=p["claims"][0]["quote"],
            document_id=UUID(p["claims"][0]["document_id"]),
            revision_id=UUID(p["claims"][0]["document_revision_id"]),
        )
        for p in (records, first, second)
    ]
    result = await query_wiki(
        _StubSession(chunks),
        context,
        kb,
        compilation,
        snapshot,
        "Records",
        mode="WIKI",
        service=service,
    )
    assert result["insufficient_evidence"] is True
    assert {e["chunk_id"] for e in result["conflicts"]} == {"alpha", "beta"}
    assert len(result["wiki_pages"]) == 3
