"""Recovered Wiki evidence reaches the model only through context admission."""

from dataclasses import replace
from uuid import UUID

import pytest

from packages.agent_runtime.evidence_registry import visible_evidence_registry
from packages.model_gateway.contracts import ModelMessage
from tests.integration.test_wiki_history_context import (
    _context,
    _history_case,
    _publish_wiki,
    _recover,
)
from tests.integration.test_wiki_history_context import (
    factory as factory,
)
from tests.integration.test_wiki_history_context import (
    pytestmark as pytestmark,
)
from tests.support.wiki import seed_wiki
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec, _model_graph


@pytest.mark.asyncio
async def test_recovered_wiki_registry_is_admitted_to_the_model_request(factory):
    context, knowledge_base_id, profile_id, _ = await seed_wiki(factory)
    context = _context(context)
    publication = await _publish_wiki(factory, context, knowledge_base_id, profile_id)
    plan, binding, turn = await _history_case(factory, context, knowledge_base_id, publication.id)
    recovered = await _recover(context, factory, binding, turn)
    assert recovered == plan.registry

    requests = []
    graph = _model_graph(request_sink=requests)
    graph.context = context
    spec = _frozen_spec(20_000)
    spec = replace(
        spec,
        model_plan=replace(
            spec.model_plan,
            primary=replace(spec.model_plan.primary, workspace_id=UUID(context.workspace_id)),
        ),
    )
    state = {
        "messages": [
            ModelMessage(role="system", content="Use only the visible cited historical evidence."),
            *turn.messages,
            ModelMessage(role="user", content="Repeat the cited retention rule."),
        ],
        "spec": spec,
        "runtime": {"max_steps": 5, "context_budget": graph._runtime_context_budget},
        "tool_definitions": {},
        "rag_evidence_registry": recovered,
        "model_round_count": 0,
        "usage_records": [],
    }
    result = await graph.model(state)

    assert result.get("failure_code") is None
    assert len(requests) == 1
    assert visible_evidence_registry(requests[0].messages, recovered) == recovered
    assert requests[0].messages[-1].content == "Repeat the cited retention rule."
