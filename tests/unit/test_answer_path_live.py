import json
from decimal import Decimal
from uuid import UUID

import pytest

from benchmarks.public_rag.answer_path_live import admit, parse_answer, request_for
from packages.model_gateway.contracts import ModelCapabilities, ResolvedModelExecutionProfile


def fixture_case(text="An evidence passage."):
    return {
        "question": "What was measured?",
        "paper_title": "Research paper",
        "answer_references": ["SECRET_REFERENCE"],
        "review_status": "SECRET_REVIEW_LABEL",
        "conditions": {
            "actual": {"records": [{"source_id": "visible", "text": text, "char_start": 7}]},
            "removed": {"records": [{"source_id": "background", "text": "Other text"}]},
        },
    }


def test_reader_receives_only_question_title_and_visible_text():
    request = request_for(fixture_case(), "actual")
    payload = json.loads(request.messages[-1].content)
    assert payload == {
        "paper_title": "Research paper",
        "records": [{"source_id": "visible", "text": "An evidence passage."}],
        "trust": "UNTRUSTED",
    }
    assert all("SECRET" not in m.content for m in request.messages)


def test_deleted_source_cannot_be_cited_in_negative_condition():
    raw = json.dumps({"answer": "A result", "unanswerable": False, "cited_source_ids": ["visible"]})
    assert parse_answer(raw, fixture_case(), "actual")["answer"] == "A result"
    with pytest.raises(ValueError, match="visible source"):
        parse_answer(raw, fixture_case(), "removed")
    refusal = json.dumps({"answer": "", "unanswerable": True, "cited_source_ids": ["background"]})
    with pytest.raises(ValueError, match="contradict"):
        parse_answer(refusal, fixture_case(), "removed")


def test_oversized_evidence_is_rejected_instead_of_silently_clipped():
    profile = ResolvedModelExecutionProfile(
        id=UUID(int=1),
        workspace_id=UUID(int=1),
        provider_credential_id=UUID(int=1),
        provider="test",
        model="test",
        temperature=Decimal(0),
        max_tokens=1024,
        timeout_seconds=Decimal(20),
        capabilities=ModelCapabilities(max_context_tokens=32768),
    )
    small = request_for(fixture_case(), "actual")
    assert admit(small, (profile,)) is small
    with pytest.raises(ValueError, match="would change"):
        admit(request_for(fixture_case("Evidence. " * 1100), "actual"), (profile,))
