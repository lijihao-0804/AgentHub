import json

import pytest

from tests.unit.test_knowledge_m3e_api import _app, _payload, _post
from tests.unit.test_knowledge_m3e_citation_qa import _FakeModelGateway, _FakeSession

QUESTION = "How many Teddy Bears were returned on Venus?"
DENIAL = "The evidence does not report how many Teddy Bears were returned on Venus."
TABLE = "Teddy Bears | Venus / Produced: 100,000 | Venus / Sold: 80,000"
BACKGROUND = (
    "The table only gives production and sales figures for Teddy Bears — "
    "Venus / Produced: 100,000 and Venus / Sold: 80,000 [1] — "
    "but no returned quantity is stated anywhere in the supplied evidence."
)


@pytest.mark.asyncio
async def test_qa_fills_a_slot_from_below_six_when_all_six_bodies_exceed_budget():
    session = _FakeSession(chunks=7)
    for chunk in session.chunks[:-1]:
        chunk.text = "What is the allowance? " + "费" * 1750
    tail = session.chunks[-1]
    tail.text = "EUR 400 with prior written approval."
    gateway = _FakeModelGateway(
        '{"answer":"EUR 400 with prior written approval [1].","citation_ids":[1],'
        '"insufficient_evidence":false}'
    )
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_max_evidence_chars = 1000
    response = await _post(app, _payload(session) | {"query": "What is the allowance?"})
    assert response.status_code == 200
    body = response.json()
    assert not body["insufficient_evidence"] and len(gateway.requests) == 1
    [citation] = body["citations"]
    assert (citation["chunk_id"], citation["excerpt"]) == (tail.chunk_id, tail.text)
    assert tail.text in gateway.requests[0].messages[1].content


@pytest.mark.asyncio
async def test_independent_shipping_count_retains_the_missing_measure_and_scope():
    question = "How many parcels were shipped in March?"
    denial = "The supplied table does not give how many parcels were shipped in March."
    aside = (
        "The table only lists orders and returns counts for parcels"
        " — March / Ordered: 60 and March / Returned: 2 [1] — "
        "but no shipped count is reported in the table."
    )
    response, gateway = await ask(
        question, denial + " " + aside, evidence="March / Ordered: 60 | March / Returned: 2"
    )
    assert response.status_code == 200
    assert len(gateway.requests) == 1
    assert response.json()["answer"] == denial and response.json()["citations"] == []


async def ask(question, answer, *, ids=(1,), contract=False, evidence=TABLE):
    session = _FakeSession(chunks=1, evidence_text=evidence)
    gateway = _FakeModelGateway(
        json.dumps(dict(answer=answer, citation_ids=list(ids), insufficient_evidence=False))
    )
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_answer_contract_v1 = contract
    return await _post(app, _payload(session) | {"query": question}), gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", [False, True])
async def test_unreported_requested_count_does_not_become_an_assertion(contract):
    response, gateway = await ask(QUESTION, DENIAL + " " + BACKGROUND, contract=contract)
    assert response.status_code == 200
    result = response.json()
    assert (result["answer"], result["insufficient_evidence"], result["citations"]) == (
        DENIAL,
        True,
        [],
    )
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (QUESTION, "Exactly zero Teddy Bears were returned on Venus [1]."),
        (QUESTION, DENIAL + " At least 10 were returned [1]."),
        (QUESTION, DENIAL + " Only returns are reported [1]."),
        (QUESTION, DENIAL + " It lists 0 returned and 40 produced [1]."),
        (QUESTION, DENIAL + " About 10, and at most 12, were returned [1]."),
        (QUESTION, DENIAL + " The table only gives 10 returns for the first week [1]."),
        (QUESTION, DENIAL + " " + BACKGROUND.replace("Sold: 80,000", "Returned: 80,000")),
        (QUESTION, DENIAL + " " + BACKGROUND + " The earlier report confirms 12 returns [1]."),
        ("Does the table report how many Teddy Bears were returned on Venus?", DENIAL + " [1]"),
        (
            "How many Teddy Bears were returned on Venus and sold on Mars?",
            DENIAL + " " + BACKGROUND,
        ),
    ],
)
async def test_partial_counts_and_supported_negative_answers_keep_their_citations(question, answer):
    response, _ = await ask(question, answer)
    assert response.status_code == 200
    body = response.json()
    assert (body["answer"], body["insufficient_evidence"]) == (answer, False)
    assert [r["id"] for r in body["citations"]] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_marker", ["[2, 3]", "[1-3]", "[ 2 ]", "[+2]", "[2，3]", "[2；3]", "[2, 3"]
)
async def test_unmapped_numeric_marker_cannot_hide_beside_a_valid_reference(bad_marker):
    response, gateway = await ask("Which locations reopened?", f"East [1]. West {bad_marker}.")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "CITATION_QA_INVALID_MODEL_OUTPUT"
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aside",
    ["It lists 40 produced and 30 sold [1].", "Only production and sales are reported [1]."],
)
async def test_only_other_named_metrics_after_an_explicit_denial_are_safe_to_omit(aside):
    response, _ = await ask(QUESTION, DENIAL + " " + aside)
    body = response.json()
    assert (body["answer"], body["insufficient_evidence"], body["citations"]) == (DENIAL, True, [])


@pytest.mark.asyncio
async def test_unmapped_numeric_sources_following_an_ordinary_noun_are_rejected():
    response, _ = await ask(
        "Which locations reopened?", "East reopened [1]. The report discusses values [2, 3]."
    )
    assert response.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "The interval is [0, 1] [1].",
        "The array is [1,2,3] [1].",
        "x ∈ [0, 1] [1].",
        "The vector x = [1, 2] [1].",
        "[0, 1] [1].",
        "数组为[1,2] [1]。",
        "The output was `[3, 5]` [1].",
    ],
)
async def test_numeric_data_remains_usable_with_an_attribution(answer):
    response, _ = await ask("What values were returned?", answer)
    assert response.status_code == 200
    assert response.json()["answer"] == answer
    assert [c["id"] for c in response.json()["citations"]] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "chinese"),
    [
        ("报销上限是多少？", True),
        ("报销上限？", True),
        ("2025 年的报销上限为几元？", True),
        ("WHO 的标准是什么？", True),
        ("WHO 指南有哪些？", True),
        ("“报销上限是多少？”", True),
        ("Does 这是什么 appear in the translation?", False),
        ("What does 《报销上限》 mean?", False),
        ("请用英文回答，报销上限是多少？", False),
        ("Please answer in Chinese. What is the limit?", True),
        ("What is the allowance?", False),
    ],
)
async def test_no_evidence_refusal_observes_question_language_without_spending(query, chinese):
    session, gateway = _FakeSession(chunks=0), _FakeModelGateway()
    response = await _post(_app(session, gateway), _payload(session) | {"query": query})
    assert response.status_code == 200
    body = response.json()
    expected = (
        "所选知识快照中的证据不足，无法回答这个问题。"
        if chinese
        else "Insufficient evidence to answer from the selected knowledge snapshot."
    )
    assert body["answer"] == expected
    assert body["insufficient_evidence"] and body["citations"] == []
    assert not gateway.requests
