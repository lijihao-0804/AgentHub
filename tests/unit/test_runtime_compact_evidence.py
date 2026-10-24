import json
from dataclasses import replace

import pytest

from packages.agent_runtime.evidence_registry import visible_evidence_registry
from packages.agent_runtime.publish import _validate_retrieval_config
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import assemble_evidence_plan, reproject_evidence_view
from packages.model_gateway.contracts import ModelMessage
from tests.unit.test_runtime_rag_evidence_projection import (
    _definition,
    _frozen_spec,
    _identifiers,
    _model_graph,
    _observation_state,
    _payload,
    _record,
)


def _compact_definition():
    original = _definition()
    return replace(
        original,
        retrieval_config={**original.retrieval_config, "evidence_projection": "compact_v1"},
    )


def test_private_identity_and_reading_metadata_remain_paired_after_tightening():
    records = [
        {**_record(1, "x" * 10_000), "document_name": "Old", "superseded": True},
        {**_record(2, "中文原文"), "effective_date": "2026-10-23", "locator": {"page": 3}},
        _record(3, "another"),
    ]
    plan = assemble_evidence_plan(
        records,
        [_identifiers(i) for i in (1, 2, 3)],
        budget=900,
        estimator=len,
        citation_prefix="R00001E",
    )
    assert plan is not None
    assert [i["id"] for i in plan.payload["results"]] == ["R00001E2", "R00001E3"]
    assert plan.payload["results"][0]["snippet"] == "中文原文"
    assert plan.payload["results"][0]["locator"] == {"page": 3}
    assert plan.payload["results"][0]["effective_date"] == "2026-10-23"
    assert "snapshot_id" not in json.dumps(plan.payload)
    assert plan.registry["R00001E2"]["reference"] == _identifiers(2)
    view = reproject_evidence_view(plan.payload, budget=400, estimator=len)
    assert view is not None and view["truncated"]
    registry = visible_evidence_registry(
        [ModelMessage("tool", json.dumps(view), "search_knowledge", "call")], plan.registry
    )
    assert set(registry) == {i["id"] for i in view["results"]}


@pytest.mark.parametrize("fault", ["missing", "duplicate", "different_kb", "ambiguous", "nan"])
def test_invalid_source_bindings_fail_closed(fault):
    record, ref = _record(1), _identifiers(1)
    records, refs = [record], [ref]
    if fault == "missing":
        refs = []
    elif fault == "duplicate":
        records, refs = [record, record], [ref, ref]
    elif fault == "different_kb":
        record["knowledge_base_id"] = "different"
    elif fault == "ambiguous":
        record.pop("snapshot_id")
        records = [record, _record(2)]
        refs = [ref, {**ref, "snapshot_id": "other"}]
    else:
        record["score"] = float("nan")
    assert assemble_evidence_plan(records, refs, budget=8_000, estimator=len) is None


def test_source_metadata_cannot_reenter_model_through_locator_or_empty_body():
    record = {
        **_record(1),
        "locator": {"page": 3, "chunk_id": "private", "nested": {"query": "hidden"}},
    }
    plan = assemble_evidence_plan([record], [_identifiers(1)], budget=8_000, estimator=len)
    assert plan is not None
    assert plan.payload["results"][0]["locator"] == {"page": 3}
    assert plan.registry["E1"]["record"]["locator"]["chunk_id"] == "private"
    for empty in ("", "   "):
        assert (
            assemble_evidence_plan(
                [{**record, "snippet": empty}], [_identifiers(1)], budget=8_000, estimator=len
            )
            is None
        )


def test_broken_search_json_or_removed_format_cannot_silently_bypass_registry():
    for content in ("{", json.dumps({"results": [{"id": "E1", "snippet": "evidence"}]})):
        with pytest.raises(AgentHubError, match="visible evidence mapping"):
            visible_evidence_registry([ModelMessage("tool", content, "search_knowledge", "c")], {})


@pytest.mark.asyncio
async def test_actual_observation_admission_model_and_second_search_keep_visible_sources():
    requests = []
    graph = _model_graph(request_sink=requests)
    definition = _compact_definition()
    records = [_record(1, "evidence"), _record(2, "x" * 2_000)]
    state = _observation_state(
        graph, definition=definition, payload=_payload(records, [_identifiers(1), _identifiers(2)])
    )
    state["messages"] = [
        ModelMessage("system", "policy"),
        ModelMessage("system", "answer"),
        ModelMessage("user", "question"),
        *state["messages"],
    ]
    state.update(await graph.observation(state))
    assert state["rag_evidence_sequence"] == 1
    assert set(state["rag_evidence_registry"]) == {"R00001E1", "R00001E2"}
    state["runtime"] = {
        "max_steps": 5,
        "context_budget": {**graph._runtime_context_budget, "max_retrieval_tokens": 550},
    }
    state["spec"] = _frozen_spec(20_000)
    update = await graph.model(state)
    assert "failure_code" not in update
    sent = json.loads(next(m.content for m in requests[0].messages if m.role == "tool"))
    assert [item["id"] for item in sent["results"]] == ["R00001E1"]
    assert set(update["rag_evidence_registry"]) == {"R00001E1"}
    assert update["rag_evidence_registry"]["R00001E1"]["record"]["snippet"] == "evidence"
    assert "evidence_refs" not in json.dumps(sent)

    second = _observation_state(
        graph, definition=definition, payload=_payload([_record(3)], [_identifiers(3)])
    )
    second.update(
        rag_evidence_sequence=state["rag_evidence_sequence"],
        rag_evidence_registry=update["rag_evidence_registry"],
    )
    # The fixture deliberately reuses the provider's call ID; namespace comes
    # from checkpoint sequence, not the provider identifier.
    second_update = await graph.observation(second)
    assert second_update["rag_evidence_sequence"] == 2
    assert "R00002E1" in second_update["rag_evidence_registry"]


@pytest.mark.asyncio
async def test_tampered_reading_view_is_rejected_before_provider_call():
    requests = []
    graph = _model_graph(request_sink=requests)
    state = _observation_state(
        graph, definition=_compact_definition(), payload=_payload([_record(1)], [_identifiers(1)])
    )
    state.update(await graph.observation(state))
    view = json.loads(state["messages"][-1].content)
    view["results"][0]["snippet"] = "forged source"
    state["messages"][-1] = replace(state["messages"][-1], content=json.dumps(view))
    state["messages"] = [
        ModelMessage("system", "policy"),
        ModelMessage("system", "answer"),
        ModelMessage("user", "question"),
        *state["messages"],
    ]
    state["runtime"]["max_steps"] = 5
    state["spec"] = _frozen_spec(20_000)
    result = await graph.model(state)
    assert result["failure_code"] == "RAG_EVIDENCE_MAPPING_INVALID"
    assert not requests


def test_published_projection_requires_explicit_supported_budget_and_preserves_defaults():
    original = _validate_retrieval_config({})
    assert _validate_retrieval_config({"evidence_projection": None}) == original
    for config in (
        {"evidence_projection": "compact_v1"},
        {"evidence_projection": "invalid", "context_budget_bytes": 8_000},
    ):
        with pytest.raises(AgentHubError, match="retrieval config"):
            _validate_retrieval_config(config)
    validated = _validate_retrieval_config(
        {"evidence_projection": "compact_v1", "context_budget_bytes": 8_000}
    )
    assert validated["evidence_projection"] == "compact_v1"


def test_invalid_checkpoint_locator_is_a_safe_mapping_error():
    plan = assemble_evidence_plan([_record(1)], [_identifiers(1)], budget=8_000, estimator=len)
    assert plan is not None
    plan.registry["E1"]["record"]["locator"] = {"type": []}
    with pytest.raises(AgentHubError, match="visible evidence mapping"):
        visible_evidence_registry(
            [ModelMessage("tool", json.dumps(plan.payload), "search_knowledge", "c")],
            plan.registry,
        )
