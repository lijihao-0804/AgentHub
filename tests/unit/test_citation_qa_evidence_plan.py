import json

import pytest

from apps.api.schemas.citation_qa import CitationQaCitationResponse
from packages.knowledge.citation_qa import CitationQaService
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS
from tests.unit.test_knowledge_m3e_citation_qa import (
    _answer,
    _FakeModelGateway,
    _FakeSession,
    _resolve_plan,
    _service,
)


@pytest.mark.asyncio
async def test_explicit_qa_view_uses_shared_plan_without_expanding_body_or_losing_reference():
    # Whole-record admission: the evidence must fit the 100-char budget
    # entirely, because records are never cut mid-string any more.
    session = _FakeSession(evidence_text="e" * 100)
    gateway = _FakeModelGateway()
    legacy = _service(session, gateway, max_evidence_chars=100)
    service = CitationQaService(
        session=session,
        retrieval_components=legacy.retrieval_components,
        model_gateway=gateway,
        max_evidence_chars=100,
        evidence_projection="compact_v1",
        model_plan_resolver=_resolve_plan,
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


def test_api_schema_and_citation_projection_share_one_excerpt_contract() -> None:
    assert CitationQaCitationResponse.model_fields["excerpt"].metadata
    assert CitationQaCitationResponse.model_validate(
        {
            "id": 1,
            "document_id": "00000000-0000-0000-0000-000000000001",
            "document_revision_id": "00000000-0000-0000-0000-000000000002",
            "chunk_id": "chunk-1",
            "source": "handbook.txt",
            "locator": {},
            "excerpt": "e" * MAX_EVIDENCE_EXCERPT_CHARS,
            "retrieval_score": 1.0,
            "rerank_score": None,
        }
    )
