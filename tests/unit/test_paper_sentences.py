from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from packages.agent_runtime.paper_sentences import (
    admit_sentence_selection,
    build_sentence_selection,
    render_sentence_selection,
    sentence_target,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelUsage,
)
from packages.model_gateway.errors import EmptyModelResponseError
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec
from tests.unit.test_verified_paper_quotes import ID, Q, S, setup_state


def test_source_offsets_cover_context_abbreviations_and_limitations():
    source = (
        "Dr. Green et al. measured 3.2 m (Fig. 4) in adults, but not in children. "
        "“No improvement.”\n\nTable 4: adjusted estimates"
    )
    selection = build_sentence_selection("What was measured?", {"R00001E2": source})
    assert list(selection.sentences) == ["S1", "S2", "S3"]
    rows = list(selection.sentences.values())
    assert "but not in children." in rows[0].text
    assert rows[2].text == "Table 4: adjusted estimates"
    for row in rows:
        assert source[row.start : row.end] == row.text
    assert render_sentence_selection('["S1", "S3"]', selection) == (
        rows[0].text + " [R00001E2]\n\n" + rows[2].text + " [R00001E2]"
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("NONE", "PAPER_SENTENCE_EVIDENCE_MISSING"),
        ("[]", "PAPER_SENTENCE_EVIDENCE_MISSING"),
        ("S40", "PAPER_SENTENCE_ID_OUT_OF_RANGE"),
        ("S1 because this is true.", "PAPER_SENTENCE_FORMAT_INVALID"),
        ("S1,S1", "PAPER_SENTENCE_FORMAT_INVALID"),
        ('{"ids":["S1"]}', "PAPER_SENTENCE_FORMAT_INVALID"),
    ],
)
def test_distinct_selection_failures(raw, code):
    selection = build_sentence_selection("what?", {ID: S})
    with pytest.raises(AgentHubError) as exc:
        render_sentence_selection(raw, selection)
    assert exc.value.code == code


def test_no_silent_tail_drop_or_id_renumbering_under_tight_budget():
    selection = build_sentence_selection(
        "what?", {ID: "First statement. " + "Another statement. " * 90}
    )
    with pytest.raises(AgentHubError) as exc:
        admit_sentence_selection(selection, _frozen_spec(20_000), {"max_retrieval_tokens": 8})
    assert exc.value.code == "PAPER_SENTENCE_BUDGET_EXCEEDED"
    assert len(selection.sentences) == 91


async def graph_state(policy="sentence_ids_v1"):
    graph, state = await setup_state()
    definition = state["tool_definitions"]["search_knowledge"]
    state["tool_definitions"]["search_knowledge"] = replace(
        definition,
        retrieval_config={**definition.retrieval_config, "paper_answer_policy": policy},
    )
    graph._emit = AsyncMock()
    return graph, state


class Gateway:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def generate_resolved(self, ctx, plan, request):
        self.requests.append(request)
        return next(self.replies)

    async def stream_resolved(self, ctx, plan, request):
        self.requests.append(request)
        response = next(self.replies)
        if response.content:
            yield ModelStreamEvent(
                ModelStreamEventType.MESSAGE_DELTA, message_delta=response.content
            )
        yield ModelStreamEvent(ModelStreamEventType.COMPLETED, response=response)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "stream"])
async def test_draft_and_selection_are_separate_metered_rounds_and_only_verified_answer_is_visible(
    mode,
):
    graph, state = await graph_state()
    graph.mode = mode
    gateway = Gateway(
        [
            ModelResponse(
                content=text, model="fixture", provider="fixture", usage=ModelUsage(100, 12, 112)
            )
            for text in ("private draft", "S2")
        ]
    )
    graph.service.model_gateway_factory = lambda session: gateway
    first = await graph.model(state)
    assert first["final_output"] is None and first["model_response"] is None
    assert graph.after_model(first) == "model"
    assert "private draft" not in repr(first)
    assert "private draft" not in repr(graph._emit.await_args_list)
    state.update(first)
    # Restore on a fresh graph: no ephemeral in-process selection state needed.
    resumed, _ = await graph_state()
    resumed.run = graph.run
    resumed.mode = mode
    resumed.service.model_gateway_factory = lambda session: gateway
    second = await resumed.model(state)
    assert second["final_output"] == S + " [" + ID + "]"
    assert second["model_round_count"] == first["model_round_count"] + 1
    assert len(second["usage_records"]) == len(first["usage_records"]) + 1
    assert [r["usage"]["total_tokens"] for r in second["usage_records"][-2:]] == [112, 112]
    assert gateway.requests[0].tools and not gateway.requests[1].tools
    assert all(
        r.request_mode is AliyunDeepSeekRequestMode.PROVIDER_DEFAULT for r in gateway.requests
    )
    assert gateway.requests[1].response_schema is None
    assert (
        "S2" in gateway.requests[1].messages[1].content
        and S in gateway.requests[1].messages[1].content
    )
    assert "private draft" not in repr(gateway.requests[1])
    assert [a.args[1]["delta"] for a in resumed._emit.await_args_list if "delta" in a.args[1]] == (
        [second["final_output"]] if mode == "stream" else []
    )
    assert resumed.after_model(second) == "finish"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "stream"])
async def test_direct_selector_keeps_receipt_when_provider_returns_no_visible_text(mode):
    graph, state = await graph_state("sentence_ids_v2")
    graph.mode = mode
    graph.step = AsyncMock()
    failure = EmptyModelResponseError(
        output_limit_reached=True,
        response=ModelResponse(
            content="",
            model="fixture",
            provider="fixture",
            finish_reason="length",
            usage=ModelUsage(75, 2048, 2123),
        ),
    )

    class TruncatedSelector(Gateway):
        def next_response(self, request):
            self.requests.append(request)
            response = next(self.replies)
            if isinstance(response, Exception):
                raise response
            return response

        async def generate_resolved(self, ctx, plan, request):
            return self.next_response(request)

        async def stream_resolved(self, ctx, plan, request):
            yield ModelStreamEvent(
                ModelStreamEventType.COMPLETED, response=self.next_response(request)
            )

    gateway = TruncatedSelector(
        [
            ModelResponse(
                content="internal draft",
                model="fixture",
                provider="fixture",
                usage=ModelUsage(50, 8, 58),
            ),
            failure,
        ]
    )
    graph.service.model_gateway_factory = lambda session: gateway
    state.update(await graph.model(state))
    second = await graph.model(state)
    assert second["failure_code"] == "MODEL_OUTPUT_TRUNCATED"
    assert [record["usage"]["output_tokens"] for record in second["usage_records"]] == [8, 2048]
    assert [request.request_mode for request in gateway.requests] == [
        AliyunDeepSeekRequestMode.PROVIDER_DEFAULT,
        AliyunDeepSeekRequestMode.SELECTOR_NON_THINKING,
    ]
    result = graph.step.await_args.args
    assert result[:2] == ("MODEL", "FAILED")
    assert result[2]["model_purpose"] == "paper_sentence_selection"
    assert (result[2]["input_tokens"], result[2]["output_tokens"]) == (75, 2048)


@pytest.mark.asyncio
async def test_normal_search_can_continue_after_first_admitted_passage():
    graph, state = await graph_state()
    gateway = Gateway(
        [
            ModelResponse(
                content="",
                tool_calls=(ModelToolCall("search_knowledge", {"query": "next comparison"}),),
                model="fixture",
                provider="fixture",
            )
        ]
    )
    graph.service.model_gateway_factory = lambda session: gateway
    update = await graph.model(state)
    assert graph.after_model(update) == "tool_proposal"
    assert not update.get("paper_sentence_selection_pending")
    assert len(gateway.requests) == 1 and gateway.requests[0].tools


@pytest.mark.asyncio
async def test_pending_selection_obeys_round_ceiling_and_requires_visible_evidence():
    graph, state = await graph_state()
    state["paper_sentence_selection_pending"] = True
    state["model_round_count"] = state["runtime"]["max_steps"]
    assert (await graph.model(state))["failure_code"] == "AGENT_MAX_STEPS_EXCEEDED"
    state["model_round_count"] = 1
    state["runtime"]["max_cost_micro_usd"] = 1000
    state["usage_records"] = [
        {"usage": None, "cost": {"amount": "0.002", "currency": "USD", "is_estimate": True}}
    ]
    stopped = await graph.model(state)
    assert stopped == {"failure_code": "AGENT_COST_LIMIT_EXCEEDED", "run_status": "NEEDS_ATTENTION"}
    state["runtime"].pop("max_cost_micro_usd")
    state["model_round_count"] = 0
    state["messages"] = [m for m in state["messages"] if m.role not in ("tool", "assistant")]
    assert (await graph.model(state))["failure_code"] == "PAPER_SENTENCE_EVIDENCE_MISSING"


@pytest.mark.asyncio
async def test_source_request_ignores_discovery_translation_and_multiple_papers():
    _, state = await graph_state()
    assert sentence_target(state, Q)
    assert sentence_target(
        state, Q.replace("how did they measure the flows", "what did the authors’ calibration show")
    )
    for q in (
        Q + " Respond in French.",
        Q.replace("how did they measure the flows", "recommend similar papers"),
        Q + " Also arXiv:2202.12121.",
    ):
        assert not sentence_target(state, q)


def test_visible_evidence_order_and_whitespace_survive_reference_only_distractors():
    import json

    target = "  A qualified fact.  Another finding.\n "
    visible = {
        "R00001E4": " A similarly named work.\n",
        "R00001E5": target,
        "R00002E1": "  An unrelated result.  ",
    }
    selection = build_sentence_selection("How?", {"R00001E5": target}, visible_sources=visible)
    groups = json.loads(selection.evidence_payload)
    assert [h for h, _ in groups] == list(visible)
    assert all("".join(text for _, text in rows) == visible[h] for h, rows in groups)
    assert groups[0][1][0][0] is None and groups[-1][1][0][0] is None
    assert {s.handle for s in selection.sentences.values()} == {"R00001E5"}
    assert selection.evidence_payload in selection.messages[1].content
    with pytest.raises(AgentHubError):
        build_sentence_selection("How?", {"R00001E5": "changed"}, visible_sources=visible)


def test_named_selector_template_is_explicit_and_preserves_previous_versions():
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.routes.agent_templates import get_workspace_context, router
    from tests.unit.test_agent_publish import _agent, _spec

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_workspace_context] = lambda: SimpleNamespace(
        permissions={"workspace_read"}
    )
    url = f"/api/v1/workspaces/{uuid4()}/agent-templates/research"
    with TestClient(app) as client:
        normal = client.get(url).json()["retrieval_config"]
        assert "paper_answer_policy" not in normal
        for name in ("sentence_ids_v1", "sentence_ids_v2", "verified_quotes_v1"):
            result = client.get(url, params={"paper_answer_policy": name})
            assert result.status_code == 200
            config = result.json()["retrieval_config"]
            frozen = _spec(_agent(retrieval_config=config))["retrieval"]
            assert all(frozen[k] == v for k, v in config.items())
            assert frozen["paper_answer_policy"] == name


def test_pending_selection_round_is_identified_in_safe_usage_metadata():
    from packages.agent_runtime.runtime_projection import _safe_step_metadata

    result = _safe_step_metadata(
        {"model_purpose": "paper_sentence_selection", "total_tokens": 42, "draft": "private"}
    )
    assert result == {"model_purpose": "paper_sentence_selection", "total_tokens": 42}


@pytest.mark.asyncio
async def test_langgraph_checkpoint_resumes_only_pending_selection():
    from langgraph.checkpoint.memory import InMemorySaver

    from packages.agent_runtime.adapters.langgraph.runtime import compile_agent_graph
    from packages.agent_runtime.runtime_projection import AgentRunState

    graph, state = await graph_state()
    gateway = Gateway(
        [
            ModelResponse(content=x, model="fixture", provider="fixture")
            for x in ("unreleased draft", "S2")
        ]
    )
    graph.service.model_gateway_factory = lambda session: gateway
    saver = InMemorySaver()

    def compiled(g):
        return compile_agent_graph(
            AgentRunState,
            nodes={"model": g.model, "finish": lambda state: {}},
            edges=(("__START__", "model"), ("finish", "__END__")),
            conditional_edges={"model": g.after_model},
            checkpointer=saver,
        )

    first = compiled(graph)
    config = {"configurable": {"thread_id": "resume-selection"}}
    await first.ainvoke(state, config, interrupt_after=["model"])
    checkpoint = await first.aget_state(config)
    assert checkpoint.values["paper_sentence_selection_pending"] and checkpoint.next == ("model",)
    assert "unreleased draft" not in repr(checkpoint.values)
    resumed, _ = await graph_state()
    resumed.run = graph.run
    resumed.service.model_gateway_factory = lambda session: gateway
    result = await compiled(resumed).ainvoke(None, config)
    assert result["final_output"] == S + " [" + ID + "]"
    assert result["model_round_count"] == 2 and len(gateway.requests) == 2
