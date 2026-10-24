from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.public_rag.consumer_replay import CONTEXT, _spec
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.evidence_set import (
    EvidenceSetSelector,
    parse_evidence_set,
    selection_messages,
)
from packages.model_gateway.contracts import ModelResponse, RetryPolicy


def sources():
    return tuple(
        RetrievedEvidence(
            f"doc{i}",
            f"rev{i}",
            f"private-chunk{i}",
            "private-source",
            {"start": i, "secret": "omit"},
            text,
            0.1 / (i + 1),
            0.9 - i * 0.1,
            {"document_name": f"Title{i}", "superseded": i == 1},
        )
        for i, text in enumerate(("A is a member of B.", "B is located in C.", "A repeats A."))
    )


def test_joint_set_uses_original_objects_and_preserves_two_distinct_orders():
    evidence = sources()
    plan = parse_evidence_set('{"selected_ids":[1,0]}', evidence, max_items=2)
    assert plan.rank_order == evidence[:2]
    assert plan.rank_order[0] is evidence[0]
    assert plan.reading_order == (evidence[1].chunk_id, evidence[0].chunk_id)
    rendered = repr(selection_messages("Where is the group A belongs to?", evidence, max_items=2))
    assert "private-chunk" not in rendered and "private-source" not in rendered
    assert "secret" not in rendered and "superseded" in rendered
    assert "B is located in C." in rendered


@pytest.mark.parametrize(
    "output",
    [
        '{"selected_ids":[true]}',
        '{"selected_ids":[3]}',
        '{"selected_ids":[0,0]}',
        '{"selected_ids":[0,1,2]}',
        '{"selected_ids":[0],"answer":"C"}',
        '{"selected_ids":[],"selected_ids":[0]}',
        '{"selected_ids":[NaN]}',
    ],
)
def test_untrusted_selector_cannot_add_facts_or_invalid_source_ids(output):
    with pytest.raises(ValueError):
        parse_evidence_set(output, sources(), max_items=2)


@pytest.mark.asyncio
async def test_selector_uses_admitted_gateway_request_once_and_propagates_invalid_output():
    plan = _spec({}).model_plan
    gateway = SimpleNamespace(
        generate_resolved=AsyncMock(
            return_value=ModelResponse('{"selected_ids":[0,1]}', "test", plan.primary.model)
        )
    )
    selector = EvidenceSetSelector(gateway, plan)
    result = await selector.select(CONTEXT, "Question?", sources(), max_items=2)
    assert len(result.rank_order) == 2
    gateway.generate_resolved.assert_awaited_once()
    gateway.generate_resolved.reset_mock()
    gateway.generate_resolved.return_value = ModelResponse(
        '{"selected_ids":[99]}', "test", plan.primary.model
    )
    with pytest.raises(ValueError):
        await selector.select(CONTEXT, "Question?", sources(), max_items=2)
    gateway.generate_resolved.assert_awaited_once()


@pytest.mark.asyncio
async def test_budget_overflow_prevents_any_provider_call():
    plan = _spec({}).model_plan
    plan = replace(
        plan,
        primary=replace(
            plan.primary, capabilities=replace(plan.primary.capabilities, max_context_tokens=1000)
        ),
    )
    gateway = SimpleNamespace(generate_resolved=AsyncMock())
    with pytest.raises((AgentHubError, ValueError)):
        await EvidenceSetSelector(gateway, plan).select(
            CONTEXT, "Question?", tuple(replace(s, text="x" * 1000) for s in sources()), max_items=2
        )
    gateway.generate_resolved.assert_not_awaited()


def test_experimental_selection_cannot_silently_retry_or_fallback():
    plan = _spec({}).model_plan
    for changed in (
        replace(plan, fallbacks=(plan.primary,)),
        replace(plan, retry_policy=RetryPolicy(max_attempts=2)),
    ):
        with pytest.raises(ValueError):
            EvidenceSetSelector(SimpleNamespace(), changed)
