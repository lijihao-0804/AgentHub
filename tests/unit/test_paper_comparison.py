from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.routes.agent_templates import _response
from apps.api.schemas.agents import RetrievalConfigRequest
from packages.agent_runtime.publish import AgentPublishService, _validate_retrieval_config
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_templates import RESEARCH_TEMPLATE, SUPPORT_TEMPLATE
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.paper_comparison import paper_comparison_query
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.tools.contracts import ToolSourceKind
from tests.unit.test_agent_publish import _agent, _spec

QUESTION = "In the workspace paper Delta, how did they compare the reviewers’ ratings in the trial?"
EXPECTED = "Delta methods and results comparing ratings between reviewers"


def test_published_research_template_enables_rewrite():
    config = RetrievalConfigRequest(**_response(RESEARCH_TEMPLATE).retrieval_config)
    assert _validate_retrieval_config(config.model_dump(exclude_none=True))["query_rewrite"] == (
        "paper_comparison_v2"
    )
    assert _response(SUPPORT_TEMPLATE).retrieval_config == {}
    assert "query_rewrite" not in _validate_retrieval_config({"query_rewrite": None})
    with pytest.raises(AgentHubError):
        _validate_retrieval_config({"query_rewrite": "unknown"})


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (QUESTION, EXPECTED),
        (
            "In the study Delta, during 2010–2014 how did they compare A and B?",
            "Delta during 2010–2014 methods and results comparing A and B",
        ),
        (
            "In the study Delta, how did they compare A and B in the 2020 trial?",
            "Delta methods and results comparing A and B in the 2020 trial",
        ),
        ("In the study Delta, how did they compare A and B? Only use 2018–2020.", None),
        ("In the study Delta, how did they compare A and B? Cite evidence from 2018 only.", None),
        (
            "For the study Echo, how did they compare losses across regions?",
            "Echo methods and results comparing losses across regions",
        ),
        (
            "Describe the comparison of delays across routes in the workspace article Beta.",
            "Beta methods and results comparing delays across routes",
        ),
        (
            "How were the acquisition costs compared in ORBIT?",
            "ORBIT methods and results comparing acquisition costs",
        ),
        (
            "How were survival rates compared between groups in the paper “Two Cohorts”?",
            "“Two Cohorts” methods and results comparing survival rates between groups",
        ),
        ("How were the results compared in the hospital?", None),
        ("In the paper arXiv:2401.12345, how did they compare A and B?", None),
        ("论文《Delta》怎样比较两组的测量值？", None),
        ("In the paper Delta, what algorithm did they use?", None),
        ("What is the difference between these datasets?", None),
        ("In the study Delta, how did they compare the result?", None),
    ],
)
def test_relation_preserving_query(question, expected):
    assert paper_comparison_query(question) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "config", "prior", "expected"),
    [
        (ToolSourceKind.BUILTIN, {"query_rewrite": "paper_comparison_v1"}, False, EXPECTED),
        (ToolSourceKind.BUILTIN, {"query_rewrite": "paper_comparison_v2"}, False, EXPECTED),
        (ToolSourceKind.BUILTIN, {}, False, "original search"),
        (ToolSourceKind.BUILTIN, {"query_rewrite": "paper_comparison_v1"}, True, "original search"),
        (ToolSourceKind.MCP, {"query_rewrite": "paper_comparison_v1"}, False, "original search"),
    ],
)
async def test_first_published_builtin_proposal_preserves_limit_and_followups(
    source, config, prior, expected
):
    graph = SimpleNamespace(
        _stop_if_requested=AsyncMock(return_value=None),
        _emit=AsyncMock(),
        step=AsyncMock(),
        run=SimpleNamespace(input_text=QUESTION),
    )
    earlier_tool = ModelMessage(
        role="tool", content="{}", name="search_knowledge", tool_call_id="earlier"
    )
    # Earlier turn evidence may be present even on the first tool call of this turn.
    history = [earlier_tool, ModelMessage(role="user", content=QUESTION)]
    calls = [
        ModelToolCall("search_knowledge", {"query": query, "limit": 3})
        for query in ("original search", "follow up")
    ]
    state = {
        "runtime": {"max_identical_calls": 2, "max_tool_calls": 4},
        "model_response": SimpleNamespace(tool_calls=calls),
        "model_round_count": 1,
        "tool_definitions": {
            "search_knowledge": SimpleNamespace(source_kind=source, retrieval_config=config)
        },
        "messages": history + ([earlier_tool] if prior else []),
    }
    result = await _AgentRunGraph.tool_proposal(graph, state)
    assert [call["arguments"] for call in result["pending_tool_calls"]] == [
        {"query": expected, "limit": 3},
        {"query": "follow up", "limit": 3},
    ]
    assert calls[0].arguments["query"] == "original search"
    proposal = graph.step.await_args.args[2]
    assert proposal["tool_call_ids"] == [
        call["tool_call_id"] for call in result["pending_tool_calls"]
    ]
    assert proposal["arguments_hashes"] == [
        canonical_json_hash(call["arguments"]) for call in result["pending_tool_calls"]
    ]


def test_same_title_queries_retain_the_distinguishing_identifier():
    for identifier in ("2301.12345", "2401.12345"):
        title = f"Shared Name (arXiv:{identifier})"
        rewritten = paper_comparison_query(f"In the paper {title}, how did they compare A and B?")
        assert rewritten == f"{title} methods and results comparing A and B"


@pytest.mark.asyncio
async def test_rewrite_survives_unrelated_draft_patch_publication_and_derivation(monkeypatch):
    agent = _agent(retrieval_config={"query_rewrite": "paper_comparison_v1"})
    service = AgentPublishService()
    session = AsyncMock()
    context = SimpleNamespace(permissions={"agent_edit"})
    monkeypatch.setattr(service, "_load_agent", AsyncMock(return_value=agent))
    await service.update_draft(session, context, agent.id, {"name": "New title"})
    spec = _spec(agent)
    assert spec["retrieval"]["query_rewrite"] == "paper_comparison_v1"
    version = SimpleNamespace(
        id=agent.id, version_number=1, resolved_spec_hash="a" * 64, resolved_spec=spec
    )
    monkeypatch.setattr(service, "get_version", AsyncMock(return_value=version))
    derived = await service.derive_draft_values(session, context, agent.id, version.id)
    assert derived["values"]["retrieval_config"]["query_rewrite"] == "paper_comparison_v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "question", "proposal", "expected"),
    [
        ("paper_comparison_v2", QUESTION, "Delta reviewer ratings trial comparison", EXPECTED),
        ("paper_comparison_v2", QUESTION, "Delta ratings across reviewers", None),
        ("paper_comparison_v2", QUESTION, "Delta reviewers’ ratings comparison", None),
        ("paper_comparison_v2", QUESTION, "Delta how did reviewer scores differ", None),
        (
            "paper_comparison_v2",
            "In the study Echo, how did they compare losses across regions?",
            "Echo regional loss differences winter cohort",
            None,
        ),
        (
            "paper_comparison_v2",
            "How were costs and delays compared in ORBIT?",
            "ORBIT expenses versus delays among sampled firms",
            None,
        ),
        (
            "paper_comparison_v1",
            "In the study Echo, how did they compare losses across regions?",
            "Echo regional loss differences winter cohort",
            "Echo methods and results comparing losses across regions",
        ),
        ("paper_comparison_v1", QUESTION, "Delta ratings across reviewers", EXPECTED),
        (
            "paper_comparison_v2",
            "In the study Echo, in 2010 how did they compare the sensors’ readings?",
            "Echo sensor reading comparison",
            "Echo in 2010 methods and results comparing readings between sensors",
        ),
    ],
)
async def test_published_query_versions_preserve_or_replace_actual_proposal(
    version, question, proposal, expected
):
    call = ModelToolCall("search_knowledge", {"query": proposal, "limit": 2}, "original-id")
    graph = SimpleNamespace(
        _stop_if_requested=AsyncMock(return_value=None),
        _emit=AsyncMock(),
        step=AsyncMock(),
        run=SimpleNamespace(input_text=question),
    )
    result = await _AgentRunGraph.tool_proposal(
        graph,
        {
            "runtime": {"max_identical_calls": 2, "max_tool_calls": 2},
            "model_response": SimpleNamespace(tool_calls=(call,)),
            "model_round_count": 1,
            "messages": [],
            "tool_definitions": {
                "search_knowledge": SimpleNamespace(
                    source_kind=ToolSourceKind.BUILTIN, retrieval_config={"query_rewrite": version}
                )
            },
        },
    )
    (actual,) = result["pending_tool_calls"]
    assert actual["tool_call_id"] == call.provider_tool_call_id
    assert actual["arguments"] == {"query": expected or proposal, "limit": 2}
    assert call.arguments == {"query": proposal, "limit": 2}
