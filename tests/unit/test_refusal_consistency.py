import json

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.citation_qa import CitationQaService
from packages.knowledge.refusal_consistency import explicit_abstention
from tests.unit.test_knowledge_m3e_citation_qa import (
    _answer,
    _FakeModelGateway,
    _FakeSession,
    _resolve_plan,
    _service,
)

QUESTION = "Which journal was used for publication?"
ABSENT = "The supplied text does not identify a journal."
BACKGROUND = "It only mentions the date of publication [1]."
CLOSING = "No journal such as Science or Nature was named."


@pytest.mark.parametrize(
    ("query", "draft", "expected"),
    [
        (QUESTION, "Insufficient evidence [1].", "Insufficient evidence."),
        (
            QUESTION,
            "The text does not identify a journal [1].",
            "The text does not identify a journal.",
        ),
        (QUESTION, f"{ABSENT} {BACKGROUND} {CLOSING}", ABSENT),
        (f'In the article "A, B and C", {QUESTION}', f"{ABSENT} {BACKGROUND} {CLOSING}", ABSENT),
        (
            "费用是多少？",
            "所提供的证据不足，无法回答这个问题 [1]。",
            "所提供的证据不足，无法回答这个问题。",
        ),
        ("费用是多少？", "年度费用没有说明，但首月为 10 元 [1]。", None),
        ("Quel journal a été utilisé ?", "Le nom du journal n’est pas indiqué [1].", None),
        (QUESTION, "They published the article in Science [1].", None),
        (QUESTION, "The supplied text does not deny that Science was used [1].", None),
        (QUESTION, "The supplied text does not rule out more than one journal [1].", None),
        (QUESTION, "The supplied text does not identify a journal other than Science [1].", None),
        (QUESTION, f"{ABSENT} It only mentions Science [1].", None),
        (
            QUESTION,
            f"{ABSENT} It only mentions the journal for the earlier work [1]. {CLOSING}",
            None,
        ),
        (QUESTION, f"{ABSENT} It only says Science; the authors confirm this [1]. {CLOSING}", None),
        (QUESTION, f"{ABSENT} {BACKGROUND} No other journal was named.", None),
        ("Which journal and conference were used?", f"{ABSENT} {BACKGROUND} {CLOSING}", None),
        (
            "Which journal was used and what issue was selected?",
            f"{ABSENT} {BACKGROUND} {CLOSING}",
            None,
        ),
        ("Which payment service was used?", f"{ABSENT} {BACKGROUND} {CLOSING}", None),
        ("Was the journal name stated?", "No. The text does not identify a journal [1].", None),
        ("Which journal was used?", "Although a volume is missing, Science is named [1].", None),
        ("Which methods did they use?", "They used a CRF [1]. Other methods are not named.", None),
        (
            "What fraction was retained?",
            "The precise fraction is unknown, but at least 20% remained [1].",
            None,
        ),
        (
            "What are the two costs?",
            "The evidence is insufficient for the total. Item A costs $5 [1].",
            None,
        ),
    ],
)
def test_declared_whole_question_abstention(query, draft, expected):
    assert explicit_abstention(query, draft) == expected


def make_service(session, gateway, **kwargs):
    return CitationQaService(
        session=session,
        retrieval_components=_service(session, gateway).retrieval_components,
        model_gateway=gateway,
        model_plan_resolver=_resolve_plan,
        max_evidence_chars=16_000,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output_mode", ["PROMPT_JSON", "JSON_SCHEMA"])
async def test_verified_abstention_uses_identical_primary_request_in_both_modes(output_mode):
    session = _FakeSession(evidence_text="The article was published in 2024.")
    answer = f"{ABSENT} {BACKGROUND} {CLOSING}"
    payload = json.dumps({"answer": answer, "citation_ids": [1], "insufficient_evidence": False})
    control, candidate = _FakeModelGateway(payload), _FakeModelGateway(payload)
    original = await _answer(
        make_service(session, control, output_mode=output_mode), session, control, query=QUESTION
    )
    fixed = await _answer(
        make_service(session, candidate, verify_refusal_consistency=True, output_mode=output_mode),
        session,
        candidate,
        query=QUESTION,
    )
    assert original.answer == answer and not original.insufficient_evidence
    assert [c.excerpt for c in original.citations] == ["The article was published in 2024."]
    assert (fixed.answer, fixed.insufficient_evidence, fixed.citations) == (ABSENT, True, ())
    assert candidate.requests == control.requests and len(candidate.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "citation_ids", "insufficient"),
    [
        ("Insufficient evidence [2].", [2], False),
        ("Insufficient evidence [1].", [], False),
        ("Insufficient evidence.", [1], False),
        ("Insufficient evidence [1].", [1], True),
    ],
)
async def test_original_citation_validation_precedes_normalization(
    answer, citation_ids, insufficient
):
    session = _FakeSession(chunks=1)
    gateway = _FakeModelGateway(
        json.dumps(
            {"answer": answer, "citation_ids": citation_ids, "insufficient_evidence": insufficient}
        )
    )
    service = make_service(
        session, gateway, verify_refusal_consistency=True, output_mode="PROMPT_JSON"
    )
    with pytest.raises(AgentHubError) as error:
        await _answer(service, session, gateway, query=QUESTION)
    assert error.value.code == "CITATION_QA_INVALID_MODEL_OUTPUT" and error.value.status_code == 502
    assert len(gateway.requests) == 2  # The existing single corrective attempt is unchanged.


@pytest.mark.asyncio
async def test_existing_citation_retry_can_resolve_to_a_supported_answer():
    session = _FakeSession(evidence_text="The paper appeared in Science.")

    class CorrectingGateway(_FakeModelGateway):
        async def generate_resolved(self, context, plan, request):
            if self.requests:
                self.content = json.dumps(
                    {
                        "answer": "They used Science [1].",
                        "citation_ids": [1],
                        "insufficient_evidence": False,
                    }
                )
            return await super().generate_resolved(context, plan, request)

    gateway = CorrectingGateway(
        '{"answer":"Insufficient evidence [33].","citation_ids":[33],"insufficient_evidence":false}'
    )
    service = make_service(
        session, gateway, verify_refusal_consistency=True, output_mode="PROMPT_JSON"
    )
    result = await _answer(service, session, gateway, query=QUESTION)
    assert not result.insufficient_evidence and result.answer == "They used Science [1]."
    assert [c.excerpt for c in result.citations] == [session.chunks[0].text]
    assert len(gateway.requests) == 2
