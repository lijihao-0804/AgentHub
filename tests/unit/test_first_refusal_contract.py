import json

import pytest

from packages.knowledge.citation_qa import resolve_citation_answer
from tests.unit.test_knowledge_m3e_api import _app, _payload, _post
from tests.unit.test_knowledge_m3e_citation_qa import _FakeModelGateway, _FakeSession

COUNT = "How many parcels were shipped in March?"
COUNT_DENIAL = "The evidence does not state how many parcels were shipped in March."
COUNT_RECAP = (
    "It only reports that 120 parcels were ordered in March and 15 were returned [1], "
    "with no information about shipments."
)
COUNT_LABELS = (
    "The evidence does not provide the number of parcels shipped in March. "
    "It only shows orders and returns figures for parcels in March "
    "(ordered 120; returned 15) [1], with no information about shipments."
)
INVENTORY = "中心配有多少台检测设备？"
INVENTORY_DENIAL = "所提供的材料中没有说明中心配有多少台检测设备。"
INVENTORY_RECAP = "资料仅提到中心配有采样设备、检测设备等设备，但未给出具体数量。[1]"
SCALAR = "北区样本的温度是多少？"
SCALAR_DENIAL = "现有证据不足以确定北区样本的温度。"
SCALAR_RECAP = (
    "证据[1]只记载南区样本的温度为十九摄氏度，并非北区样本；"
    "证据[2]虽提到北区样本已登记，但说明当时温度计送修，未给出温度数值。"
)
PERSON = "In 2020, who was selected as the new chair of the city where Bridges are located?"
PERSON_RECAP = (
    "The evidence states that Bridges are located in Durham [1], "
    "but none of the supplied evidence identifies who was selected as Durham's new chair in 2020. "
    "The only 2020 selection mentioned concerns Leeds, not Durham [2]. "
    "Therefore, the evidence is insufficient to answer the question."
)


def raw(answer, ids, insufficient):
    return json.dumps(dict(answer=answer, citation_ids=ids, insufficient_evidence=insufficient))


@pytest.mark.parametrize(
    ("query", "answer", "expected"),
    [
        (
            "What is the title of the leaflet?",
            "The source does not give the title of the leaflet [1]. It only lists its publisher.",
            "The source does not give the title of the leaflet.",
        ),
        (
            "Who operated the switch?",
            "The document does not identify who operated the switch [1].",
            "The document does not identify who operated the switch.",
        ),
        (
            "When did the workshop open?",
            "The opening date is not recorded in the report [1]. See Appendix B.",
            "The opening date is not recorded in the report.",
        ),
        (
            "How much did the operator pay for the replacement?",
            "The report gives no information about the fee [1]. The fee was not listed [1].",
            "The report gives no information about the fee.",
        ),
        (
            "车站何时开放？",
            "报告未给出日期[1]。见附表三。",
            "报告未给出日期。",
        ),
        (
            "How many volunteers joined the inspection?",
            "The report does not state how many volunteers joined the inspection. "
            "It only notes that volunteers attended the opening [1].",
            "The report does not state how many volunteers joined the inspection.",
        ),
        (
            "区域中心每年支付多少元？",
            "所提供证据无法回答该问题。材料仅说明服务仍需续订[1]，但没有给出每年的具体数字。",
            "所提供证据无法回答该问题。",
        ),
        (
            "Who leads the company that researchers are affiliated with?",
            "The evidence reports that researchers are affiliated with Orion [1], "
            "but it does not identify who leads Orion.",
            "The evidence is insufficient to answer the question.",
        ),
    ],
)
def test_independent_missing_fields_and_bridge_predicates(query, answer, expected):
    receipt = resolve_citation_answer(
        query,
        raw(answer, [], True),
        visible_ids={1},
        output_limit_reached=False,
        first_refusal_contract=True,
    )
    assert (receipt.answer, receipt.insufficient_evidence, receipt.citation_ids) == (
        expected,
        True,
        (),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["PROMPT_JSON", "JSON_SCHEMA"])
@pytest.mark.parametrize(
    ("query", "answer", "ids", "declared", "expected"),
    [
        (COUNT, COUNT_DENIAL + " " + COUNT_RECAP, [1], True, COUNT_DENIAL),
        (COUNT, COUNT_LABELS, [], True, COUNT_LABELS.split(". ")[0] + "."),
        (INVENTORY, INVENTORY_DENIAL + INVENTORY_RECAP, [1], False, INVENTORY_DENIAL),
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP, [], True, SCALAR_DENIAL),
        (
            PERSON,
            PERSON_RECAP,
            [1, 2],
            True,
            "The evidence is insufficient to answer the question.",
        ),
    ],
)
async def test_first_refusal_uses_same_request_and_returns_without_correction(
    mode, query, answer, ids, declared, expected
):
    session = _FakeSession(chunks=2)
    payload = _payload(session) | {"query": query, "output_mode": mode}
    gateway = _FakeModelGateway(raw(answer, ids, declared))
    app = _app(session, gateway)
    assert not app.state.settings.knowledge_qa_first_refusal_contract
    baseline = await _post(app, payload)
    first_request = gateway.requests[0]
    if declared:
        assert baseline.status_code == 502
    else:
        assert baseline.json()["answer"] == answer and baseline.json()["citations"]

    gateway.requests.clear()
    app.state.settings.knowledge_qa_first_refusal_contract = True
    result = await _post(app, payload)
    assert gateway.requests == [first_request]
    assert result.status_code == 200
    body = result.json()
    assert (body["answer"], body["insufficient_evidence"], body["citations"]) == (
        expected,
        True,
        [],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "answer"),
    [
        ("资料有没有说明中心配有多少台检测设备？", INVENTORY_DENIAL + INVENTORY_RECAP),
        ("中心配有多少台检测设备和采样设备？", INVENTORY_DENIAL + INVENTORY_RECAP),
        (INVENTORY, INVENTORY_DENIAL + INVENTORY_RECAP.replace("采样设备、", "至少三台检测设备、")),
        (INVENTORY, "中心没有任何检测设备 [1]。"),
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP.replace("十九摄氏度", "十九度，北区约十度")),
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP.replace("已登记", "已超过零度")),
        ("北区样本和南区样本的温度是多少？", SCALAR_DENIAL + SCALAR_RECAP),
        (COUNT, COUNT_DENIAL + " " + COUNT_RECAP.replace("were returned", "were shipped")),
        (COUNT, COUNT_LABELS.replace("returned 15", "shipped 15")),
        (COUNT, "None were shipped in March [1]. The April figure is unknown."),
        ("Does the report state how many parcels were shipped in March?", COUNT_DENIAL + " [1]"),
        (PERSON, PERSON_RECAP.replace("not Durham", "also Durham")),
        (PERSON, PERSON_RECAP.replace("new chair in 2020", "new chair in 2021")),
        (PERSON.replace("who was", "which city and who was"), PERSON_RECAP),
    ],
)
async def test_supported_negative_scopes_bounds_and_partial_answers_keep_attribution(query, answer):
    ids = [n for n in (1, 2) if f"[{n}]" in answer]
    session = _FakeSession(chunks=2)
    gateway = _FakeModelGateway(raw(answer, ids, False))
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_first_refusal_contract = True
    result = await _post(app, _payload(session) | {"query": query})
    assert result.status_code == 200 and len(gateway.requests) == 1
    body = result.json()
    assert (body["answer"], body["insufficient_evidence"]) == (answer, False)
    assert [item["id"] for item in body["citations"]] == ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "answer", "ids"),
    [
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP, [3]),
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP.replace("[1]", "[3]"), []),
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP.replace("[1]", "[1,2]"), []),
        (SCALAR, SCALAR_DENIAL + SCALAR_RECAP, [1]),
        (INVENTORY, INVENTORY_DENIAL, [1]),
        ("北区样本和南区样本的温度是多少？", SCALAR_DENIAL + SCALAR_RECAP, []),
        (PERSON, PERSON_RECAP + " A council member says it was Jones [2].", [1, 2]),
        (
            "What was the ticket price in York?",
            "The ticket price in Leeds is not recorded [1].",
            [1],
        ),
        (
            "When did the gate open?",
            "The date is not reported [1]. It had already opened before June [1].",
            [1],
        ),
        (
            "How many volunteers joined the inspection?",
            "The report does not state how many volunteers joined the inspection. "
            "It only notes that a pair joined the inspection [1].",
            [1],
        ),
    ],
)
async def test_unproven_or_invalid_declared_refusal_uses_normal_validation(query, answer, ids):
    session = _FakeSession(chunks=2)
    gateway = _FakeModelGateway(raw(answer, ids, True))
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_first_refusal_contract = True
    result = await _post(app, _payload(session) | {"query": query, "output_mode": "PROMPT_JSON"})
    assert result.status_code == 502 and len(gateway.requests) == 2
    assert result.json()["error"]["code"] == "CITATION_QA_INVALID_MODEL_OUTPUT"


def test_receipt_retains_original_text_and_rule_for_replay():
    content = raw(INVENTORY_DENIAL + INVENTORY_RECAP, [1], False)
    receipt = resolve_citation_answer(
        INVENTORY, content, visible_ids={1}, output_limit_reached=False, first_refusal_contract=True
    )
    assert receipt.raw_content == content and receipt.rule == "first_whole_refusal"


@pytest.mark.parametrize(
    "query", ["根据原文，温度是多少？", "According to the heading, who leads?"]
)
def test_source_preface_does_not_turn_a_refusal_into_a_quotation(query):
    content = raw("Insufficient evidence [1].", [1], False)
    receipt = resolve_citation_answer(
        query,
        content,
        visible_ids={1},
        output_limit_reached=False,
        first_refusal_contract=True,
        evidence_texts={1: "The room was empty."},
    )
    assert receipt.insufficient_evidence and not receipt.citation_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "label"),
    [
        ("What message appeared on the screen?", "Insufficient evidence"),
        ("Which verdict was entered in the evidence log?", "Insufficient evidence"),
        ("请照录状态栏的原文。", "证据不足"),
        ("调查结论栏里填了什么？", "证据不足"),
    ],
)
async def test_grounded_literal_message_remains_cited(question, label):
    answer = f"{label} [1]."
    session = _FakeSession(evidence_text=f"Log entry: “{label}”.")
    gateway = _FakeModelGateway(raw(answer, [1], False))
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_first_refusal_contract = True
    body = (await _post(app, _payload(session) | {"query": question})).json()
    assert (body["answer"], body["insufficient_evidence"]) == (answer, False)
    assert [c["id"] for c in body["citations"]] == [1]


def test_evidence_present_in_an_uncited_record_cannot_make_a_refusal_a_literal_answer():
    content = raw("Insufficient evidence [1].", [1], False)
    receipt = resolve_citation_answer(
        "Which verdict prevailed?",
        content,
        visible_ids={1, 2},
        output_limit_reached=False,
        first_refusal_contract=True,
        evidence_texts={1: "No entry.", 2: "The paper read 'Insufficient evidence'."},
    )
    assert receipt.insufficient_evidence and not receipt.citation_ids
