import hashlib
import json

import pytest

from packages.knowledge.citation_qa import (
    _InvalidCitationAnswer,
    citation_system_prompt,
    resolve_citation_answer,
)
from tests.unit.test_knowledge_m3e_citation_qa import _answer, _FakeModelGateway, _FakeSession
from tests.unit.test_refusal_consistency import make_service


def resolve(question, body, ids, flag):
    raw = json.dumps(dict(answer=body, citation_ids=ids, insufficient_evidence=flag))
    receipt = resolve_citation_answer(
        question, raw, visible_ids={2, 5}, output_limit_reached=False, answer_contract_v1=True
    )
    assert receipt.raw_content == raw
    assert receipt.raw_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    return receipt


@pytest.mark.parametrize("flag,ids", [(False, [2]), (True, [2]), (True, []), (True, [5])])
def test_explicit_pure_refusal_can_have_redundant_in_range_markers(flag, ids):
    receipt = resolve(
        "Can these documents establish ownership?", "Insufficient evidence [2].", ids, flag
    )
    assert (receipt.answer, receipt.citation_ids, receipt.insufficient_evidence) == (
        "Insufficient evidence.",
        (),
        True,
    )
    assert receipt.rule == "pure_whole_refusal"


@pytest.mark.parametrize(
    "body,ids,flag",
    [
        ("Insufficient evidence [4].", [], True),
        ("Insufficient evidence [2].", [7], True),
        ("Insufficient evidence [2].", [5], False),
        ("Insufficient evidence [2,5].", [], True),
        ("Insufficient evidence [+2].", [], True),
        ("Insufficient evidence [02].", [], True),
        ("The trial stopped [2].", [5], False),
        ("Insufficient evidence [2].", [True], True),
        ("Insufficient evidence [2].", [2, 2], True),
        ("Insufficient evidence [2].", [2], "true"),
        ("They did not use a placebo [2].", [2], True),
    ],
)
def test_original_reference_and_schema_errors_survive(body, ids, flag):
    with pytest.raises(_InvalidCitationAnswer):
        resolve("Which controls were used?", body, ids, flag)


@pytest.mark.parametrize(
    "body",
    [
        "No. Treatment did not reduce the error [5].",
        "The tower is at least 80 m [2]. Its exact height is unknown.",
        "Only the eastern station closed [2]. The western station's status is unknown.",
        "The change cost €19 [5] except for renewals, whose price was not reported.",
    ],
)
def test_negative_partial_and_limited_answers_keep_sources(body):
    ids = [2] if "[2]" in body else [5]
    receipt = resolve("What did the report establish?", body, ids, False)
    assert (receipt.answer, receipt.insufficient_evidence, receipt.rule) == (body, False, None)


def test_navigation_only_denial_preserves_scope_and_refuses_to_drop_conditions():
    question = "Which criteria were used for admission?"
    denial = "The supplied evidence does not specify the admission criteria."
    aside = (
        " The report notes that details are presented in chapter 7 [5], "
        "but the excerpts do not include that chapter or name any admission criteria."
    )
    assert resolve(question, denial + aside, [5], True).answer == denial
    for request, tail in [
        ("Which criteria were used and where are they listed?", aside),
        (question, aside.replace("details", "criteria and exceptions")),
        (question, aside + " Applicants younger than 18 had to reapply [2]."),
    ]:
        with pytest.raises(_InvalidCitationAnswer):
            resolve(request, denial + tail, [5], True)


def test_recognized_conflict_with_source_facts_is_rejected_without_erasing_them():
    body = (
        "The evidence does not describe how Alloy X differs from bronze. "
        "It only states that Alloy X contains tin [2]. No comparison to bronze is provided."
    )
    with pytest.raises(_InvalidCitationAnswer) as err:
        resolve("How does Alloy X differ from bronze?", body, [2], False)
    assert err.value.reason == "state_body_conflict"
    contrasted = body.replace("contains tin", "contains less tin than bronze")
    assert (
        resolve("How does Alloy X differ from bronze?", contrasted, [2], False).answer == contrasted
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["JSON_SCHEMA", "PROMPT_JSON"])
async def test_candidate_first_response_goes_through_service_with_no_correction(mode):
    session = _FakeSession(evidence_text="A price was not recorded.")
    raw = json.dumps(
        dict(answer="Insufficient evidence [1].", citation_ids=[1], insufficient_evidence=True)
    )
    gateway = _FakeModelGateway(raw)
    service = make_service(session, gateway, answer_contract_v1=True, output_mode=mode)
    result = await _answer(service, session, gateway, query="What is the total price?")
    assert result.answer_receipt.raw_content == raw and not result.citations
    assert len(gateway.requests) == 1
    assert gateway.requests[0].messages[0].content == citation_system_prompt(
        mode, answer_contract_v1=True
    )
    assert citation_system_prompt(mode) != gateway.requests[0].messages[0].content
    bad = _FakeModelGateway(raw.replace("[1]", "[9]"))
    with pytest.raises(_InvalidCitationAnswer):
        await _answer(
            make_service(session, bad, answer_contract_v1=True, output_mode=mode), session, bad
        )
    assert len(bad.requests) == 1


@pytest.mark.parametrize(
    "question,body",
    [
        (
            "What are the differences between cedar and pine?",
            "The report only mentions a cedar sample [2]. No direct comparison with pine is given.",
        ),
        (
            "Compare cedar to pine.",
            "No comparison to pine is available. It only describes the cedar treatment [2].",
        ),
        (
            "请比较甲方案与乙方案的差异？",
            "材料仅提到甲方案保留原始记录 [2]。现有资料未提供二者的比较。",
        ),
    ],
)
def test_conflicting_state_with_multiple_question_forms(question, body):
    with pytest.raises(_InvalidCitationAnswer) as error:
        resolve(question, body, [2], False)
    assert error.value.reason == "state_body_conflict"


@pytest.mark.parametrize(
    "question,body",
    [
        (
            "Compare cedar to pine and describe preparation.",
            "No comparison to pine is available. It only describes the cedar treatment [2].",
        ),
        (
            "请比较甲方案与乙方案的差异？",
            "材料仅提到甲方案比乙方案更快 [2]。现有资料未提供二者的比较。",
        ),
        (
            "What are the differences between cedar and pine?",
            "Cedar scored twice as high [2]. No direct comparison with pine is given.",
        ),
    ],
)
def test_supported_or_separate_requested_fact_is_preserved(question, body):
    assert resolve(question, body, [2], False).answer == body


@pytest.mark.parametrize(
    "raw",
    [
        '{"answer":"Insufficient evidence.","answer":"other",'
        '"citation_ids":[],"insufficient_evidence":true}',
        '{"answer":true,"citation_ids":[],"insufficient_evidence":true}',
        '["Insufficient evidence."]',
    ],
)
def test_strict_json_before_any_repair(raw):
    with pytest.raises(_InvalidCitationAnswer):
        resolve_citation_answer(
            "Which compound?",
            raw,
            visible_ids={1},
            output_limit_reached=False,
            answer_contract_v1=True,
        )


@pytest.mark.parametrize(
    "body", ["Yes, admission is guaranteed.", "是的，已确认该条款。", "Insufficient evidence [2."]
)
def test_unsupported_flag_or_dangling_numeric_marker_cannot_pass(body):
    with pytest.raises(_InvalidCitationAnswer):
        resolve("Was the clause accepted?", body, [], True)
