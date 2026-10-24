import json

import pytest

from packages.knowledge.citation_qa import CitationQaService
from tests.unit.test_knowledge_m3e_citation_qa import (
    _answer,
    _FakeModelGateway,
    _FakeSession,
    _service,
)


@pytest.mark.asyncio
async def test_explicit_qa_view_uses_shared_plan_without_expanding_body_or_losing_reference():
    session = _FakeSession(evidence_text="e" * 2_000)
    gateway = _FakeModelGateway()
    legacy = _service(session, gateway, max_evidence_chars=100)
    service = CitationQaService(
        session=session,
        retrieval_components=legacy.retrieval_components,
        model_gateway=gateway,
        max_evidence_chars=100,
        evidence_projection="compact_v1",
    )
    result = await _answer(service, session, gateway)
    content = gateway.requests[0].messages[1].content
    view = json.loads(content.split("Untrusted reference data:\n", 1)[1])
    assert view["evidence_format"] == "rag_view_v1"
    assert view["results"][0]["id"] == "1"
    assert view["results"][0]["snippet"] == "e" * 100
    assert view["results"][0]["document_name"] == session.document_models[0].name
    assert "chunk_id" not in json.dumps(view)
    assert result.citations[0].chunk_id == session.chunks[0].chunk_id
    assert result.citations[0].excerpt == "e" * 100
    assert gateway.requests[0].required_capabilities.required == frozenset({"structured_output"})
