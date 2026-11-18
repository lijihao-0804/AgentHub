from __future__ import annotations

import json
from dataclasses import replace

import pytest

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.knowledge.citation_qa import CitationQaService
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.joint_evidence import select_joint_evidence
from tests.unit.test_knowledge_m3e_api import _app, _payload, _post
from tests.unit.test_knowledge_m3e_citation_qa import (
    _answer,
    _FakeModelGateway,
    _FakeSession,
    _plan,
    _resolver_for,
    _service,
)


def _chunk(rank, text, doc=None, start=0):
    source = doc or f"source-{rank}"
    return RetrievedEvidence(
        source,
        source + "-rev",
        f"chunk-{rank}",
        source,
        {"type": "text_range", "char_start": start, "char_end": start + len(text)},
        text,
        1 / rank,
        1 - rank / 20,
        {},
    )


QUESTION = "In which region was the designer of the Kestrel Relay born?"
FACTS = (
    "The Kestrel Relay was designed by Orla Venn, its sole designer.",
    "The Kestrel Relay has removable copper contact plates.",
    "A handbook for repairing the Kestrel Relay includes wiring diagrams.",
    "The manufacturing region supplies parts by rail in winter.",
    "Another designer patented an adjustable mounting bracket.",
    "The archive lists the year the Kestrel Relay passed inspection.",
    "Orla Venn was born in the Velden region and grew up near its port.",
    "A fair in the region sells porcelain models of historic airships.",
)


def test_off_query_bridge_completes_two_different_facts_without_gold_or_generated_text():
    ranked = tuple(_chunk(i, sentence) for i, sentence in enumerate(FACTS, 1))
    selected = select_joint_evidence(QUESTION, ranked, max_items=2)
    assert len(selected) == 2 and selected[0] is ranked[0] and selected[1] is ranked[6]
    assert select_joint_evidence(QUESTION, ranked) == select_joint_evidence(QUESTION, ranked)
    # Metadata supplied by a benchmark or document has no authority over the selection.
    renamed = tuple(
        replace(p, chunk_id=f"opaque-{i}", metadata={"is_supporting": i == 3})
        for i, p in enumerate(ranked)
    )
    assert [p.text for p in select_joint_evidence(QUESTION, renamed, max_items=2)] == [
        p.text for p in selected
    ]


def test_repeated_assertions_and_repeated_ids_do_not_fill_slots_or_imply_a_second_hop():
    first = _chunk(1, "Rowan Pike engraved the first copper plate at noon.")
    duplicate = _chunk(2, first.text, doc="second-source")
    independent = _chunk(3, "The plate was delivered to the courthouse in winter.")
    assert select_joint_evidence(
        "What happened to the plate?", (first, duplicate, independent, first)
    ) == (first, independent)


def test_continuity_uses_comparable_original_coordinates_and_skips_overlapping_copies():
    first = _chunk(1, "Alder Beacon was first used for coast surveys in the north.", doc="log")
    adjacent = _chunk(
        7,
        "north. It closed when the last ships transferred to the inland channel.",
        doc="log",
        start=len(first.text) - 6,
    )
    decoy = _chunk(5, "Other coast surveys continued throughout the rainy season.")
    query = "What happened to the Alder Beacon coast surveys?"
    assert select_joint_evidence(query, (first, decoy, adjacent), max_items=2) == (first, adjacent)
    on_another_page = replace(adjacent, locator=dict(adjacent.locator, page=2))
    assert select_joint_evidence(query, (first, decoy, on_another_page), max_items=2) == (
        first,
        decoy,
    )


def test_whole_originals_obey_language_aware_body_budget_and_pool_and_slot_caps():
    estimator = ApproxTokenEstimator()
    small = tuple(_chunk(i, "证据" + str(i)) for i in range(2, 23))
    oversized = _chunk(1, "测" * 1000)
    ranked = (oversized, *small)
    budget = sum(estimator.estimate(p.text) for p in small[:3])
    selected = select_joint_evidence("证据", ranked, max_tokens=budget)
    assert selected and sum(estimator.estimate(p.text) for p in selected) <= budget
    assert all(any(item is original for original in ranked[1:20]) for item in selected)
    assert len(select_joint_evidence("证据", ranked)) <= 6
    assert select_joint_evidence("证据", ranked, max_tokens=0) == ()
    with pytest.raises(ValueError):
        select_joint_evidence("证据", ranked, max_items=7)


@pytest.mark.asyncio
async def test_actual_qa_messages_select_from_same_reranked_pool_and_resolve_every_visible_id():
    session = _FakeSession(chunks=len(FACTS))
    for row, body in zip(session.chunks, FACTS, strict=True):
        row.text = body
        row.locator = {"type": "text_range", "char_start": 0, "char_end": len(body)}
    content = json.dumps(
        {
            "answer": "Grounds [1][2][3][4][5][6].",
            "citation_ids": list(range(1, 7)),
            "insufficient_evidence": False,
        }
    )

    class Ranked:
        def rerank(self, _query, candidates):
            return tuple(1 - int(p.chunk_id.split("-")[1]) / 20 for p in candidates)

    base = _service(session, _FakeModelGateway())
    components = replace(base.retrieval_components, reranker=Ranked())
    results, requests = [], []
    for enabled in (False, True):
        gateway = _FakeModelGateway(content)
        service = CitationQaService(
            session=session,
            retrieval_components=components,
            model_gateway=gateway,
            max_evidence_chars=16_000,
            joint_six_v1=enabled,
            model_plan_resolver=base.model_plan_resolver,
        )
        results.append(await _answer(service, session, gateway, query=QUESTION))
        assert len(gateway.requests) == 1
        requests.append(gateway.requests[0])
    baseline, candidate = results
    assert [c.chunk_id for c in baseline.citations] == [f"chunk-{i}" for i in range(1, 7)]
    assert baseline.retrieval_trace.evidence_set is None
    assert baseline.retrieval_trace.selection.results == candidate.retrieval_trace.selection.results
    assert requests[0].messages[0] == requests[1].messages[0]
    stage = candidate.retrieval_trace.evidence_set
    assert stage.metadata["candidate_count"] == len(FACTS)
    assert [r.chunk_id for r in stage.results] == [c.chunk_id for c in candidate.citations]
    model_text = requests[1].messages[1].content
    assert (
        FACTS[0] in model_text
        and FACTS[6] in model_text
        and FACTS[6] not in requests[0].messages[1].content
    )
    for citation in candidate.citations:
        row = next(row for row in session.chunks if row.chunk_id == citation.chunk_id)
        assert f"[{citation.id}]\nsource: {citation.source}\n" in model_text
        assert citation.excerpt == row.text and row.text in model_text
        assert citation.locator == row.locator and citation.document_revision_id == str(
            row.document_revision_id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", [None, "compact_v1"])
async def test_smaller_model_window_repacks_complete_originals_before_first_dispatch(projection):
    session = _FakeSession(chunks=2)
    session.chunks[0].text = "Hydrograph. " * 600
    session.chunks[1].text = "The gauge reads zero."
    gateway = _FakeModelGateway()
    base = _service(session, gateway)
    service = CitationQaService(
        session=session,
        retrieval_components=base.retrieval_components,
        model_gateway=gateway,
        max_evidence_chars=16_000,
        joint_six_v1=True,
        evidence_projection=projection,
        model_plan_resolver=_resolver_for(_plan(context_tokens=1_600, max_tokens=200)),
    )
    result = await _answer(service, session, gateway)
    assert len(gateway.requests) == 1
    message = gateway.requests[0].messages[1].content
    assert "Hydrograph" not in message and session.chunks[1].text in message
    assert [c.chunk_id for c in result.citations] == ["chunk-2"]


@pytest.mark.asyncio
async def test_http_flag_retains_complete_long_citation_and_exposes_actual_delivery_stage():
    body = "A recorded observation. " * 100 + "The final reading was 17."
    session = _FakeSession(evidence_text=body)
    gateway = _FakeModelGateway()
    app = _app(session, gateway)
    assert app.state.settings.knowledge_qa_joint_six_v1 is False
    app.state.settings.knowledge_qa_joint_six_v1 = True
    response = await _post(app, _payload(session))
    assert response.status_code == 200
    result = response.json()
    assert len(body) > 2_000 and result["citations"][0]["excerpt"] == body
    assert body in gateway.requests[0].messages[1].content
    assert (
        result["retrieval_trace"]["stages"]["evidence_set"]["results"][0]["chunk_id"]
        == session.chunks[0].chunk_id
    )
