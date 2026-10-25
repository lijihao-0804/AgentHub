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
