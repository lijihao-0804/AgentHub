import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from apps.api.routes.agent_templates import get_workspace_context, router
from apps.api.schemas.agents import AgentCreateRequest
from packages.agent_runtime.paper_quotes import quote_sources, render_quotes
from packages.agent_runtime.publish import _validate_retrieval_config
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
)
from tests.unit.test_agent_publish import _agent, _spec
from tests.unit.test_runtime_compact_evidence import _compact_definition
from tests.unit.test_runtime_rag_evidence_projection import (
    _frozen_spec,
    _identifiers,
    _model_graph,
    _observation_state,
    _payload,
    _record,
)

Q = (
    "In the workspace paper Delta (arXiv:2401.12345), how did they measure the flows? "
    "Cite supporting passages from this workspace."
)
S = "The team’s meter recorded hourly flows."
ID = "R00001E1"


def selection(text=S, handle=ID):
    return json.dumps([{"evidence_id": handle, "quote": text}])


async def setup_state(question=Q, *, enabled=True, name="2401.12345.txt"):
    graph = _model_graph(request_sink=[])
    graph.run.input_text = question
    definition = _compact_definition()
    if enabled:
        definition = replace(
            definition,
            retrieval_config={
                **definition.retrieval_config,
                "paper_answer_policy": "verified_quotes_v1",
            },
        )
    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(
            [{**_record(1, "An earlier pilot did not continue. " + S), "document_name": name}],
            [_identifiers(1)],
        ),
    )
    state["messages"] = [
        ModelMessage("system", "policy"),
        ModelMessage("system", "research"),
        ModelMessage("user", question),
        *state["messages"],
    ]
    state.update(await graph.observation(state))
    state["runtime"] = {"max_steps": 5, "context_budget": graph._runtime_context_budget}
    state["spec"] = _frozen_spec(20_000)
    return graph, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "valid"), [("sync", True), ("stream", True), ("sync", False), ("stream", False)]
)
async def test_real_graph_never_exposes_an_unverified_final_or_selection_json(mode, valid):
    graph, state = await setup_state()
    graph.mode = mode
    graph._emit = AsyncMock()
    seen = []
    raw = (
        selection(S.replace("’", "'"))
        if valid
        else selection("The flows increased by five percent.")
    )
    response = ModelResponse(content=raw, provider="fixture", model="fixture")

    class Gateway:
        async def generate_resolved(self, context, plan, request):
            seen.append(request)
            return response

        async def stream_resolved(self, context, plan, request):
            seen.append(request)
            yield ModelStreamEvent(ModelStreamEventType.MESSAGE_DELTA, message_delta=raw)
            yield ModelStreamEvent(ModelStreamEventType.COMPLETED, response=response)

    graph.service.model_gateway_factory = lambda session: Gateway()
    update = await graph.model(state)
    assert "JSON array" in seen[0].messages[0].content
    assert "JSON array" not in state["messages"][0].content
    deltas = [c.args[1]["delta"] for c in graph._emit.await_args_list if "delta" in c.args[1]]
    if valid:
        assert update["final_output"] == f"{S} [{ID}]"
        assert deltas == ([update["final_output"]] if mode == "stream" else [])
    else:
        assert update["failure_code"] == "PAPER_QUOTE_SELECTION_INVALID"
        assert deltas == []
        assert not update.get("final_output")


@pytest.mark.asyncio
async def test_source_eligibility_checks_current_turn_document_identity_and_request_scope():
    graph, state = await setup_state()
    assert quote_sources(state, state["messages"], Q)
    assert not quote_sources(state, [*state["messages"], ModelMessage("user", Q)], Q)
    for q in [
        Q.replace("Delta (arXiv:2401.12345)", "Other"),
        Q.replace(" Cite supporting passages from this workspace.", ""),
        Q.replace(
            "how did they measure the flows", "respond in French and explain the measurements"
        ),
        Q + " Answer in Chinese.",
        "请说明论文结果并引用原文。",
        "Which articles should I consult? Cite supporting passages from this workspace.",
    ]:
        messages = [replace(m, content=q) if m.role == "user" else m for m in state["messages"]]
        assert not quote_sources(state, messages, q)
    _, disabled = await setup_state(enabled=False)
    assert not quote_sources(disabled, disabled["messages"], Q)


@pytest.mark.parametrize(
    "raw",
    [
        selection("did not continue."),
        selection(S, "R99999E1"),
        selection("The team did not record flows."),
        json.dumps([{"evidence_id": ID, "quote": S, "comment": "Unverified."}]),
        selection() + " More.",
        selection().replace('"quote":', '"quote":"ignored","quote":'),
        "[]",
        json.dumps([{"evidence_id": ID, "quote": S}] * 5),
    ],
)
def test_malformed_wrongly_attributed_and_partial_selections_fail_closed(raw):
    assert render_quotes(raw, {ID: "The original pilot did not continue. " + S}) is None


@pytest.mark.asyncio
async def test_wrong_document_duplicate_revisions_and_wiki_cannot_supply_a_quote():
    graph, state = await setup_state()
    other = _observation_state(
        graph,
        definition=state["tool_definitions"]["search_knowledge"],
        payload=_payload(
            [{**_record(2, "Only another paper reports this."), "document_name": "other.txt"}],
            [_identifiers(2)],
        ),
    )
    other.update(rag_evidence_sequence=1, rag_evidence_registry=state["rag_evidence_registry"])
    updated = await graph.observation(other)
    messages = [*state["messages"], updated["messages"][-1]]
    registry = updated["rag_evidence_registry"]
    combined = {**state, "rag_evidence_registry": registry}
    assert list(quote_sources(combined, messages, Q)) == [ID]
    # Same filename on two immutable revisions makes the target ambiguous.
    h = "R00002E1"
    registry[h]["record"]["document_name"] = "2401.12345.txt"
    payload = json.loads(messages[-1].content)
    payload["results"][0]["document_name"] = "2401.12345.txt"
    messages[-1] = replace(messages[-1], content=json.dumps(payload))
    assert not quote_sources(combined, messages, Q)
    messages[-1] = replace(messages[-1], name="query_wiki")
    assert list(quote_sources(combined, messages, Q)) == [ID]


def test_multiple_complete_sentences_keep_their_source_handles():
    second = "The second meter only ran at night."
    raw = json.dumps(
        [
            {"evidence_id": ID, "quote": S.replace(" ", "  ")},
            {"evidence_id": "R00001E2", "quote": second},
        ]
    )
    assert render_quotes(raw, {ID: S, "R00001E2": second}) == f"{S} [{ID}]\n\n{second} [R00001E2]"


def test_policy_requires_explicit_compact_projection_and_a_budget():
    assert "paper_answer_policy" not in _validate_retrieval_config({})
    with pytest.raises(AgentHubError):
        _validate_retrieval_config({"paper_answer_policy": "verified_quotes_v1"})
    config = {
        "paper_answer_policy": "verified_quotes_v1",
        "evidence_projection": "compact_v1",
        "context_budget_bytes": 8000,
    }
    assert _validate_retrieval_config(config)["paper_answer_policy"] == "verified_quotes_v1"


def test_template_selection_roundtrips_and_leaves_the_ordinary_default_off():
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_workspace_context] = lambda: SimpleNamespace(
        permissions={"workspace_read"}
    )
    path = f"/api/v1/workspaces/{uuid4()}/agent-templates/research"
    with TestClient(app) as client:
        normal = client.get(path)
        selected = client.get(path, params={"paper_answer_policy": "verified_quotes_v1"})
        assert normal.status_code == selected.status_code == 200
        assert "paper_answer_policy" not in normal.json()["retrieval_config"]
        assert client.get(path, params={"paper_answer_policy": "unknown"}).status_code == 422
    template = selected.json()
    draft = AgentCreateRequest.model_validate(
        {
            "name": template["name"],
            "system_prompt": template["system_prompt"],
            "model_profile_id": uuid4(),
            "retrieval_config": template["retrieval_config"],
        }
    )
    frozen = _spec(_agent(retrieval_config=draft.retrieval_config.model_dump(exclude_none=True)))[
        "retrieval"
    ]
    assert all(frozen[k] == v for k, v in template["retrieval_config"].items())
    assert frozen["paper_answer_policy"] == "verified_quotes_v1"
