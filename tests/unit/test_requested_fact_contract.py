import json
from dataclasses import replace

import pytest

from apps.api.knowledge_dependencies import get_citation_qa_model_plan_resolver
from packages.knowledge.citation_qa import (
    _REQUESTED_FACT_CORRECTION,
    citation_system_prompt,
    resolve_citation_answer,
)
from tests.unit.test_knowledge_m3e_api import _app, _payload, _post
from tests.unit.test_knowledge_m3e_citation_qa import (
    _FakeModelGateway,
    _FakeSession,
    _plan,
    _resolver_for,
)


def supported(*claims, **extra):
    return {
        "kind": "supported",
        "claims": [{"text": text, "citation_ids": ids} for text, ids in claims],
        **extra,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["PROMPT_JSON", "JSON_SCHEMA"])
async def test_only_opted_in_prompt_protocol_changes_its_system_message(mode):
    session = _FakeSession(chunks=1)
    payload = _payload(session) | {"query": "Was the gate closed?", "output_mode": mode}
    requests = []
    for enabled in (False, True):
        structured = enabled and mode == "PROMPT_JSON"
        raw = (
            supported(("Yes.", [1]))
            if structured
            else {"answer": "Yes [1].", "citation_ids": [1], "insufficient_evidence": False}
        )
        gateway = _FakeModelGateway(json.dumps(raw))
        app = _app(session, gateway)
        assert not app.state.settings.knowledge_qa_requested_fact_contract
        app.state.settings.knowledge_qa_requested_fact_contract = enabled
        response = await _post(app, payload)
        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == "Yes [1]." and body["citations"][0]["chunk_id"] == "chunk-1"
        [request] = gateway.requests
        requests.append(request)
    old, new = requests
    if mode == "JSON_SCHEMA":
        assert new == old
    else:
        assert new.response_schema is None
        assert new.messages[0].content == citation_system_prompt(mode, requested_fact_contract=True)
        assert new.messages[0] != old.messages[0]
        assert replace(new, messages=(old.messages[0], *new.messages[1:])) == old


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "How many were issued?",
            "Insufficient evidence to answer from the selected knowledge snapshot.",
        ),
        ("具体发了多少份？", "所选知识快照中的证据不足，无法回答这个问题。"),
        (
            "请用英文回答，具体发了多少份？",
            "Insufficient evidence to answer from the selected knowledge snapshot.",
        ),
    ],
)
async def test_only_kind_is_needed_to_refuse_locally(query, expected):
    session = _FakeSession(evidence_text="There is a limit of 30.")
    gateway = _FakeModelGateway('{"kind":"insufficient"}')
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_requested_fact_contract = True
    resp = await _post(app, _payload(session) | {"query": query, "output_mode": "PROMPT_JSON"})
    assert resp.status_code == 200 and len(gateway.requests) == 1
    body = resp.json()
    assert (body["answer"], body["insufficient_evidence"], body["citations"]) == (
        expected,
        True,
        [],
    )


@pytest.mark.asyncio
async def test_claims_receive_their_own_locatable_sources_and_keep_an_explicit_missing_part():
    session = _FakeSession(chunks=2)
    query = "Who distributed the remaining items, how many were there, and who approved them?"
    raw = supported(
        ("The cooperative distributed them.", [1]),
        ("There were four.", [2, 1]),
        unanswered_parts=["who approved them"],
    )
    gateway = _FakeModelGateway(json.dumps(raw))
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_requested_fact_contract = True
    resp = await _post(app, _payload(session) | {"query": query, "output_mode": "PROMPT_JSON"})
    assert resp.status_code == 200 and len(gateway.requests) == 1
    body = resp.json()
    assert not body["insufficient_evidence"]
    assert body["answer"] == (
        "The cooperative distributed them [1]. There were four [1][2]. "
        "The evidence does not answer “who approved them”."
    )
    assert [item["id"] for item in body["citations"]] == [1, 2]
    by_chunk = {c.chunk_id: c for c in session.chunks}
    prompt = gateway.requests[0].messages[1].content
    for item in body["citations"]:
        chunk = by_chunk[item["chunk_id"]]
        section = prompt.split(f"\n[{item['id']}]\n", 1)[1].split("\n[", 1)[0]
        assert chunk.text in section and str(chunk.document_revision_id) in section
        assert item["locator"] == chunk.locator and item["excerpt"] == chunk.text


@pytest.mark.parametrize(
    ("text", "source"),
    [
        ("No.", "The activity is prohibited."),
        ("Zero.", "The final total was zero."),
        ("The maximum is 30.", "Up to 30 requests are permitted."),
        ("Insufficient evidence.", 'The status line reads "Insufficient evidence".'),
        ("[404]", "The label is [404]."),
        ("The status code is [404].", "The reported code was [404]."),
        ("The array is [0, 1].", "Output: [0, 1]."),
        ("数组为[1,2]。", "数据为[1,2]。"),
    ],
)
def test_supported_branch_preserves_negatives_bounds_and_literal_values(text, source):
    content = json.dumps(supported((text, [3])))
    receipt = resolve_citation_answer(
        "What does the record say?",
        content,
        visible_ids={3},
        output_limit_reached=False,
        requested_fact_contract=True,
        evidence_texts={3: source},
    )
    assert receipt.raw_content == content
    assert not receipt.insufficient_evidence and receipt.citation_ids == (3,)
    assert receipt.answer.replace(" [3]", "") == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "calls"),
    [
        ('{"kind":"insufficient","claims":[]}', 1),
        ('{"kind":"insufficient","kind":"supported"}', 1),
        (json.dumps(supported()), 1),
        (json.dumps(supported(("Fact.", []))), 1),
        (json.dumps(supported(("Fact.", [True]))), 1),
        (json.dumps(supported(("Fact.", ["1"]))), 1),
        (json.dumps(supported(("Fact.", [2]))), 2),
        (json.dumps(supported(("Fact.", [1, 1]))), 2),
        (json.dumps(supported(("Fact [1].", [1]))), 2),
        (json.dumps(supported(("Fact [2, 3].", [1]))), 2),
        (json.dumps(supported(("Fact [9", [1]))), 2),
        (json.dumps(supported(("The code is [777].", [1]))), 2),
        (json.dumps(supported(("Fact.", [1]), unanswered_parts=["an invented unknown fact"])), 2),
        ('{"answer":"Yes [1].","citation_ids":[1],"insufficient_evidence":false}', 1),
    ],
)
async def test_invalid_branch_shape_or_source_cannot_reach_the_public_answer(raw, calls):
    session = _FakeSession(chunks=1)
    gateway = _FakeModelGateway(raw)
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_requested_fact_contract = True
    response = await _post(app, _payload(session) | {"output_mode": "PROMPT_JSON"})
    assert response.status_code == 502 and len(gateway.requests) == calls
    assert response.json()["error"]["code"] == "CITATION_QA_INVALID_MODEL_OUTPUT"
    if calls == 2:
        assert gateway.requests[1].messages[-1].content == _REQUESTED_FACT_CORRECTION


@pytest.mark.asyncio
async def test_corrected_claim_can_use_the_same_schema_and_be_delivered():
    class CorrectingGateway(_FakeModelGateway):
        async def generate_resolved(self, *args):
            self.content = json.dumps(supported(("Zero.", [1 if self.requests else 5])))
            return await super().generate_resolved(*args)

    session, gateway = _FakeSession(chunks=1), CorrectingGateway()
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_requested_fact_contract = True
    resp = await _post(app, _payload(session) | {"output_mode": "PROMPT_JSON"})
    assert resp.status_code == 200 and resp.json()["answer"] == "Zero [1]."
    first, retry = gateway.requests
    assert retry.messages[:2] == first.messages
    assert retry.messages[-1].content == _REQUESTED_FACT_CORRECTION


@pytest.mark.asyncio
async def test_candidate_uses_selected_evidence_for_data_labels_and_runs_the_context_budget():
    session = _FakeSession(evidence_text="The entry label was [404].")
    gateway = _FakeModelGateway(json.dumps(supported(("The label is [404].", [1]))))
    app = _app(session, gateway)
    app.state.settings.knowledge_qa_requested_fact_contract = True
    payload = _payload(session) | {"query": "What label was printed?", "output_mode": "PROMPT_JSON"}
    resp = await _post(app, payload)
    assert resp.status_code == 200
    assert resp.json()["answer"] == "The label is [404] [1]."
    assert [(item["id"], item["excerpt"]) for item in resp.json()["citations"]] == [
        (1, session.chunks[0].text)
    ]
    assert session.chunks[0].text in gateway.requests[0].messages[1].content

    gateway.requests.clear()
    app.dependency_overrides[get_citation_qa_model_plan_resolver] = lambda: _resolver_for(
        _plan(context_tokens=1_000, max_tokens=200)
    )
    resp = await _post(app, payload | {"query": "问题" * 1900})
    assert resp.json()["error"]["code"] == "CITATION_QA_CONTEXT_BUDGET_EXCEEDED"
    assert not gateway.requests
