import json

import pytest

from benchmarks.retrieval.experiments.bilingual_refusal_100.policy import expected_prompt, policy
from packages.core.config.settings import Settings
from packages.knowledge import citation_qa
from tests.unit.test_knowledge_m3e_citation_qa import _answer, _FakeModelGateway, _FakeSession
from tests.unit.test_refusal_consistency import make_service


@pytest.mark.parametrize(
    "question,language",
    [
        ("Who funded the exhibition?", "English"),
        ("How much did 《北京》 cost?", "English"),
        ("What does 東京 mean here?", "English"),
        ("第一阶段的总成本是多少？", "Chinese"),
        ("研究使用了哪些 Python 库？", "Chinese"),
        ("「What happened」是哪一年的作品？", "Chinese"),
        ("誰負責 OpenAI 專案？", "Chinese"),
        ("¿Cuánto costó el equipo?", None),
        ("Que signifie «What» ici ?", None),
        ("東京の開館日はいつですか？", None),
        ("Was kostet das Gerät?", None),
        ("北京 / Oslo 2025?", None),
        ("What changed? 请问哪些指标有变化？", None),
    ],
)
def test_request_grammar_selects_one_template_or_defers_to_the_question(question, language):
    selected = citation_qa._question_language_hint(question)
    assert selected == language
    prompt = expected_prompt("candidate", question)
    if language is None:
        assert all(text not in prompt for text in citation_qa._REFUSAL_SENTENCES.values())
        assert "language requested by the user" in prompt
    else:
        example = json.loads(prompt.splitlines()[-1])
        assert example == {
            "answer": citation_qa._REFUSAL_SENTENCES[language],
            "citation_ids": [],
            "insufficient_evidence": True,
        }
        assert sum(text in prompt for text in citation_qa._REFUSAL_SENTENCES.values()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,prose,ids,flag",
    [
        ("Who funded the project?", "Insufficient evidence to answer this question.", [], True),
        ("项目的预算是多少？", "目前提供的材料不足以回答。", [], True),
        ("What is the French title?", "The title is «Le Jour» [1].", [1], False),
        ("开馆时间和总成本分别是多少？", "六月开馆 [1]。材料没有给出总成本。", [1], False),
        ("Did it open in May?", "No. It opened in June [1].", [1], False),
        ("Est-ce que le prix est indiqué ?", "Le prix n’est pas indiqué.", [], True),
        ("What is the answer?", "证据不足，无法回答这个问题。", [], True),
    ],
)
async def test_primary_gateway_request_carries_policy_and_receipt_preserves_actual_model_text(
    question, prose, ids, flag
):
    raw = json.dumps(
        {"answer": prose, "citation_ids": ids, "insufficient_evidence": flag}, ensure_ascii=False
    )
    gateway = _FakeModelGateway(raw)
    session = _FakeSession(
        evidence_text="The archive opened in June. Its French title was «Le Jour»."
    )
    service = make_service(
        session, gateway, output_mode="PROMPT_JSON", question_language_prompt=True
    )
    result = await _answer(service, session, gateway, query=question)
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.messages[0].content == expected_prompt("candidate", question)
    assert request.messages[1].content.startswith("Question:\n" + question + "\n")
    assert (result.answer, result.insufficient_evidence) == (prose, flag)
    assert [c.id for c in result.citations] == ids
    assert result.answer_receipt.raw_content == raw
    assert result.answer_receipt.rule is None


@pytest.mark.asyncio
async def test_correction_keeps_the_first_model_text_and_rejects_refusal_citations():
    raw = '{"answer":"不知道 [1]。","citation_ids":[1],"insufficient_evidence":true}'
    session, gateway = _FakeSession(evidence_text="六月开馆。"), _FakeModelGateway(raw)
    with pytest.raises(citation_qa._InvalidCitationAnswer, match="citation") as err:
        await _answer(
            make_service(
                session, gateway, output_mode="PROMPT_JSON", question_language_prompt=True
            ),
            session,
            gateway,
            query="谁资助了该项目？",
        )
    assert err.value.reason == "citations_on_refusal"
    assert len(gateway.requests) == 2
    assert gateway.requests[1].messages[:2] == gateway.requests[0].messages
    assert gateway.requests[1].messages[2].content == raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_mode,contract", [("JSON_SCHEMA", False), ("JSON_SCHEMA", True), ("PROMPT_JSON", True)]
)
async def test_distinct_output_contracts_keep_their_existing_prompt(output_mode, contract):
    raw = '{"answer":"June [1].","citation_ids":[1],"insufficient_evidence":false}'
    session, gateway = (
        _FakeSession(evidence_text="The archive opened in June."),
        _FakeModelGateway(raw),
    )
    await _answer(
        make_service(
            session,
            gateway,
            output_mode=output_mode,
            answer_contract_v1=contract,
            question_language_prompt=True,
        ),
        session,
        gateway,
        query="When did it open?",
    )
    assert gateway.requests[0].messages[0].content == citation_qa.citation_system_prompt(
        output_mode, answer_contract_v1=contract
    )


def test_experiment_uses_the_product_setting_and_restores_it():
    assert Settings.model_fields["knowledge_qa_question_language_prompt"].default is False
    settings = Settings(_env_file=None, knowledge_qa_question_language_prompt=False)
    assert expected_prompt("baseline", "谁？") == citation_qa._PROMPT_JSON_SYSTEM
    with policy("candidate", settings=settings):
        assert settings.knowledge_qa_question_language_prompt
    assert not settings.knowledge_qa_question_language_prompt


@pytest.mark.asyncio
async def test_empty_chinese_snapshot_has_a_local_response_and_no_model_attempt():
    session, gateway = _FakeSession(chunks=0), _FakeModelGateway("")
    service = make_service(
        session, gateway, output_mode="PROMPT_JSON", question_language_prompt=True
    )
    response = await _answer(service, session, gateway, query="谁资助了该项目？")
    assert response.answer == citation_qa._REFUSAL_SENTENCES["Chinese"]
    assert response.insufficient_evidence and response.citations == ()
    assert not gateway.requests
    assert response.answer_receipt is None
