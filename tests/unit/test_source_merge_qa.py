"""Integration across hydrated ranking, both QA message formats, and HTTP projection."""

import json
from dataclasses import replace

import pytest

from apps.api.routes.citation_qa import _response
from packages.knowledge.adapters.fakes import DeterministicFakeReranker
from packages.knowledge.citation_qa import CitationQaService
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.source_merge import _join
from tests.unit.test_knowledge_m3e_citation_qa import (
    _answer,
    _FakeModelGateway,
    _FakeSession,
    _plan,
    _resolver_for,
    _service,
)


def source(start, text, **extra):
    return {"type": "text_range", "char_start": start, "char_end": start + len(text), **extra}


def test_coordinates_require_matching_scope_span_and_overlap_content():
    first = RetrievedEvidence("d", "r", "a", "src", source(0, "abcd"), "abcd", 1, None, {})
    second = replace(first, chunk_id="b", locator=source(3, "def"), text="def")
    assert _join(second, first)[0].text == "abcdef"
    assert _join(first, replace(second, locator=source(4, "ef"), text="ef"))[0].text == "abcdef"
    for invalid in (
        replace(second, document_revision_id="other"),
        replace(second, document_id="other"),
        replace(second, text="zef"),
        replace(second, text="shorter?"),
        replace(second, locator=source(5, "def")),
        replace(second, locator=source(3, "def", block_index=1)),
        replace(second, locator={"page": 1}),
    ):
        assert _join(first, invalid) is None


def fixture(monkeypatch, *, budget=16000, projection=None, enabled=True, context_tokens=16000):
    session = _FakeSession(chunks=7)
    text = "a" * 1300 + "b" * 1000 + " KEY SOURCE TAIL"
    session.document_models[1] = session.document_models[0]
    session.revision_models[1] = session.revision_models[0]
    for i, chunk in enumerate(session.chunks):
        start = 1200 if i == 1 else 0
        chunk.text = [text[:1300], text[1200:]][i] if i < 2 else f"source {i + 1}"
        chunk.locator = source(start, chunk.text)
    monkeypatch.setattr(
        DeterministicFakeReranker,
        "rerank",
        lambda _, query, candidates: tuple(
            -float(item.chunk_id.split("-")[-1]) for item in candidates
        ),
    )
    gateway = _FakeModelGateway()
    qa = CitationQaService(
        session=session,
        retrieval_components=_service(session, gateway).retrieval_components,
        model_gateway=gateway,
        max_evidence_chars=budget,
        source_merge_refill_v1=enabled,
        evidence_projection=projection,
        model_plan_resolver=_resolver_for(_plan(context_tokens=context_tokens, max_tokens=150)),
    )
    return qa, session, gateway, text


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", [None, "compact_v1"])
async def test_full_composite_and_new_source_survive_message_and_http(monkeypatch, projection):
    qa, session, gateway, text = fixture(monkeypatch, projection=projection)
    result = _response(await _answer(qa, session, gateway))
    prompt = gateway.requests[0].messages[1].content
    assert text in prompt and "source 7" in prompt
    if projection:
        items = json.loads(prompt.split("Untrusted reference data:\n")[1])["results"]
        assert len(items) == 6 and len(items[0]["locator"]["components"]) == 2
    else:
        assert prompt.count("\ntext:\n") == 6
    cite = result.citations[0]
    assert cite.excerpt == text
    assert [p.chunk_id for p in cite.components] == ["chunk-1", "chunk-2"]
    for p in cite.components:
        assert p.excerpt == text[p.locator["char_start"] : p.locator["char_end"]]


@pytest.mark.asyncio
async def test_body_guard_preserves_old_visible_chunks(monkeypatch):
    qa, session, gateway, _ = fixture(monkeypatch, budget=400)
    await _answer(qa, session, gateway)
    old, session2, baseline, _ = fixture(monkeypatch, budget=400, enabled=False)
    await _answer(old, session2, baseline)
    prompt = gateway.requests[0].messages[1].content
    assert "source 7" not in prompt and "source_union" not in prompt
    assert (
        session.chunks[0].text in prompt
        and session2.chunks[0].text in baseline.requests[0].messages[1].content
    )


@pytest.mark.asyncio
async def test_frozen_context_guard_reuses_admissible_baseline(monkeypatch):
    qa, session, gateway, _ = fixture(monkeypatch)
    # A rank-seven source can fit the body limit but not the model's context.
    session.chunks[-1].text = "new evidence " * 2500
    qa.model_plan_resolver = _resolver_for(_plan(context_tokens=3000, max_tokens=150))
    result = await _answer(qa, session, gateway)
    assert not result.citations[0].components
    prompt = gateway.requests[0].messages[1].content
    assert session.chunks[0].text in prompt and "new evidence" not in prompt
