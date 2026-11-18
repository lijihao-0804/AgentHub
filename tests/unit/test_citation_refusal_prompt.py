import hashlib
import json

import pytest

from benchmarks.retrieval.citation_refusal_policy import policy
from packages.knowledge import citation_qa
from tests.unit.test_knowledge_m3e_citation_qa import _answer, _FakeModelGateway, _FakeSession
from tests.unit.test_refusal_consistency import make_service


def test_paired_prompt_reproduces_the_original_and_restores_the_factory():
    factory = citation_qa.citation_system_prompt
    with policy("baseline"):
        original = citation_qa.citation_system_prompt("PROMPT_JSON")
        assert hashlib.sha256(original.encode()).hexdigest() == (
            "9921923abe84909a433d1333667a6cb16f7ba0928080ec0b21136d1b12238499"
        )
    with policy("candidate"):
        candidate = citation_qa.citation_system_prompt("PROMPT_JSON")
        with pytest.raises(ValueError, match="require PROMPT_JSON"):
            citation_qa.citation_system_prompt("JSON_SCHEMA")
        with pytest.raises(ValueError, match="answer_contract_v1 off"):
            citation_qa.citation_system_prompt("PROMPT_JSON", answer_contract_v1=True)
    assert citation_qa.citation_system_prompt is factory
    assert candidate != original
    assert candidate == factory("PROMPT_JSON")
    assert factory("JSON_SCHEMA") == citation_qa._SYSTEM_PROMPT
    assert candidate not in factory("PROMPT_JSON", answer_contract_v1=True)


@pytest.mark.asyncio
async def test_prompt_json_refusal_example_passes_the_service_verbatim_in_one_attempt():
    prompt = citation_qa.citation_system_prompt("PROMPT_JSON")
    example = prompt.splitlines()[-1]
    refusal = json.loads(example)
    session = _FakeSession(evidence_text="The archive opened in June.")
    gateway = _FakeModelGateway(example)
    result = await _answer(
        make_service(session, gateway, output_mode="PROMPT_JSON"),
        session,
        gateway,
        query="Who funded the archive?",
    )
    assert len(gateway.requests) == 1 and gateway.requests[0].messages[0].content == prompt
    assert result.insufficient_evidence and result.citations == ()
    assert result.answer == refusal["answer"]
    assert result.answer_receipt.raw_content == example and result.answer_receipt.rule is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,answer",
    [
        ("Did the archive open in May?", "No. The archive opened in June [1]."),
        (
            "When did the archive open and who funded it?",
            "It opened in June [1]. Its funding source is unknown.",
        ),
    ],
)
async def test_cited_negative_and_partial_first_answers_keep_their_evidence(query, answer):
    evidence = "The archive opened in June."
    session = _FakeSession(evidence_text=evidence)
    raw = json.dumps({"answer": answer, "citation_ids": [1], "insufficient_evidence": False})
    gateway = _FakeModelGateway(raw)
    result = await _answer(
        make_service(session, gateway, output_mode="PROMPT_JSON"), session, gateway, query=query
    )
    assert len(gateway.requests) == 1 and not result.insufficient_evidence
    assert result.answer == answer and result.answer_receipt.rule is None
    assert [(c.id, c.excerpt) for c in result.citations] == [(1, evidence)]


@pytest.mark.asyncio
@pytest.mark.parametrize("listed_ids", [[], [1]])
async def test_refusal_with_cited_background_is_never_accepted_verbatim(listed_ids):
    session = _FakeSession(evidence_text="The archive opened in June.")
    raw = json.dumps(
        {
            "answer": "The funder is unknown. The archive opened in June [1].",
            "citation_ids": listed_ids,
            "insufficient_evidence": True,
        }
    )
    gateway = _FakeModelGateway(raw)
    with pytest.raises(citation_qa._InvalidCitationAnswer) as rejected:
        await _answer(
            make_service(session, gateway, output_mode="PROMPT_JSON"),
            session,
            gateway,
            query="Who funded the archive?",
        )
    assert rejected.value.reason == "citations_on_refusal" and rejected.value.status_code == 502
    assert len(gateway.requests) == 2
    assert gateway.requests[1].messages[-2].content == raw
