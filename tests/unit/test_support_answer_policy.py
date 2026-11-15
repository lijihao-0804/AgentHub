import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.api.schemas.agents import RuntimeConfigRequest
from benchmarks.evaluation.experiments.support_refund_rag.fixture_tools import (
    FixtureCaseScope,
    case_scope,
    get_refund_status_handler,
)
from packages.agent_runtime.publish import _validate_runtime_config
from packages.agent_runtime.support_facts import (
    SUPPORT_FACT_RULES,
    support_observation,
    support_system_prompt,
)
from packages.core.errors.exceptions import AgentHubError
from packages.tools.contracts import ToolApprovalPolicy, ToolDefinition, ToolEffect, ToolRisk


def definition(name, *, required=("reference",), optional=()):
    return ToolDefinition(
        identity=name,
        revision_id=uuid4(),
        spec_hash="abc",
        description="lookup",
        input_schema={
            "properties": {key: {"type": "string"} for key in (*required, *optional)},
            "required": list(required),
            "additionalProperties": False,
        },
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=10,
    )


def test_published_policy_and_schema_specific_capability():
    tools = [
        definition("lookup", required=("parcel",), optional=("branch",)),
        replace(
            definition("open_case"),
            effect=ToolEffect.WRITE,
            approval_policy=ToolApprovalPolicy.ALWAYS,
        ),
    ]
    assert "support_answer_policy" not in _validate_runtime_config({})
    assert "support_answer_policy" not in _validate_runtime_config({"support_answer_policy": None})
    assert support_system_prompt("original", {}, tools) == "original"
    with pytest.raises(AgentHubError):
        _validate_runtime_config({"support_answer_policy": "future"})
    request = RuntimeConfigRequest(support_answer_policy="support_facts_v1")
    runtime = _validate_runtime_config(request.model_dump(exclude_none=True))
    message = support_system_prompt("original", runtime, tools)
    capability = json.loads(message.rsplit("\n", 1)[1])
    assert capability[0] == {
        "tool": "lookup",
        "requires": ["parcel"],
        "accepts": ["branch", "parcel"],
        "additionalProperties": False,
        "effect": "READ",
        "approval": "NEVER",
    }
    assert capability[1]["approval"] == "ALWAYS"
    assert support_system_prompt("original", runtime, []).endswith("[]")


@pytest.mark.parametrize(
    ("data", "refund", "application"),
    [
        ({}, "MISSING", "MISSING"),
        ({"refund": None, "refund_requested_at": "2032-04-11"}, "EXPLICIT_NULL", "RETURNED"),
        ({"refund": [], "refund_requested_at": None}, "EMPTY_LIST", "EXPLICIT_NULL"),
        (
            {
                "refund": {
                    "updated_at": "2031-07-11",
                    "requested_amount": 18,
                    "approved_amount": 12,
                },
                "amounts": {"order_total": 29, "paid_amount": 24},
                "business_time": "2031-07-12",
            },
            "RETURNED",
            "MISSING",
        ),
    ],
)
def test_projection_preserves_field_presence_and_semantics(data, refund, application):
    payload = {"status": "SUCCESS", "data": data}
    projected = support_observation(payload, definition("get_order"), 4000)
    assert projected["data"] == data
    assert projected["observation"] == {
        "scope": "this_tool_response",
        "refund": refund,
        "refund_requested_at": application,
    }
    assert support_observation(payload, definition("get_order"), 1) == payload


@pytest.mark.parametrize(
    ("data", "state", "coverage"),
    [
        ({}, "MISSING", None),
        ({"open_tickets": []}, "EMPTY_LIST", None),
        (
            {
                "open_ticket_scope": {"subject": "parcel"},
                "open_tickets_complete": False,
                "open_tickets": [],
            },
            "EMPTY_LIST",
            "partial",
        ),
        (
            {
                "open_ticket_scope": {"subject": "parcel"},
                "open_tickets_complete": True,
                "open_tickets": [],
            },
            "EMPTY_LIST",
            "complete_for_returned_scope",
        ),
    ],
)
def test_scope_requires_matching_upstream_guarantee(data, state, coverage):
    info = support_observation(
        {"status": "SUCCESS", "data": data}, definition("query_customer"), 800
    )["observation"]
    assert info["open_tickets"] == state
    assert info.get("coverage") == coverage


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ERROR", "error_code": "UPSTREAM_TIMEOUT"},
        {"status": "SUCCESS", "truncated": True, "data": {"preview": "[]"}},
        {
            "status": "SUCCESS",
            "data": {"ticket_ref": "W-7"},
            "approval_decision": "APPROVED",
            "approval_execution": "SUCCEEDED",
        },
        {"status": "ERROR", "approval_decision": "DENIED", "approval_execution": "NOT_STARTED"},
        {
            "status": "ERROR",
            "approval_decision": "APPROVED",
            "approval_execution": "UNKNOWN_OUTCOME",
        },
    ],
)
def test_no_new_facts_for_incomplete_observation_or_write(payload):
    assert support_observation(payload, definition("open_case"), 800) is payload
    if payload.get("truncated") or payload["status"] == "ERROR":
        assert support_observation(payload, definition("get_order"), 800) is payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts",
    [
        {"refund_requested_at": "2032-04-11"},
        {"refund": None},
        {"refund": []},
    ],
)
async def test_candidate_read_preserves_absent_vs_null_and_application(facts):
    order = {"order_id": "P-94", "customer_id": "C-8", **facts}
    handler = get_refund_status_handler(store=SimpleNamespace(get=lambda _: order))
    with case_scope(
        FixtureCaseScope("alternative", "P-94", "C-8", support_answer_policy="support_facts_v1")
    ):
        result = await handler(None, None, {"order_id": "P-94"}, None)
    assert {k: result[k] for k in ("refund", "refund_requested_at") if k in result} == facts
    with case_scope(FixtureCaseScope("alternative", "P-94", "C-8")):
        legacy = await handler(None, None, {"order_id": "P-94"}, None)
    assert legacy["refund"] == facts.get("refund")
    assert "refund_requested_at" not in legacy


def test_policy_generation_guidance_retains_both_logical_directions():
    assert "OR alternatives" in SUPPORT_FACT_RULES and "AND requirements" in SUPPORT_FACT_RULES
    assert "exceptions, channel/product scope and window trigger" in SUPPORT_FACT_RULES
    assert (
        "only if the cited policy expressly makes it exclusive for this scope" in SUPPORT_FACT_RULES
    )
    assert "exact visible passage supporting it" in SUPPORT_FACT_RULES


def test_effective_read_schema_and_write_governance_override_raw_defaults():
    lookup = replace(definition("parcel"), input_schema={"properties": {"parcel_id": {}}})
    action = replace(definition("dispatch"), effect=ToolEffect.WRITE)
    prompt = support_system_prompt(
        "base", {"support_answer_policy": "support_facts_v1"}, [action, lookup]
    )
    caps = {item["tool"]: item for item in json.loads(prompt.rsplit("\n", 1)[1])}
    assert caps["parcel"]["additionalProperties"] is False
    assert caps["dispatch"]["approval"] == "ALWAYS"


@pytest.mark.parametrize(
    "snippet",
    [
        "冷藏货物可以凭承运回执或门店交接单登记。",
        "维修补助要求单号和质检报告同时齐备；在外地维修的除外。",
        "此处仅限旧机补贴；该补贴唯一申报入口是线下服务站。",
        "普通维修可通过线下服务站申报，另有电话登记。",
    ],
)
def test_candidate_never_changes_condition_or_citation_ownership(snippet):
    from packages.agent_runtime.rag_evidence import project_rag_evidence_payload

    refs = [
        {
            "chunk_id": ident,
            "document_revision_id": "rev-" + ident,
            "snapshot_id": "snapshot",
            "knowledge_base_id": "kb",
        }
        for ident in ("a", "b")
    ]
    records = [
        {
            "chunk_id": ref["chunk_id"],
            "document_revision_id": ref["document_revision_id"],
            "snippet": text,
        }
        for ref, text in zip(refs, (snippet, "本段仅说明受理后的通知渠道。"), strict=True)
    ]
    admitted = project_rag_evidence_payload(
        {"status": "SUCCESS", "data": {"results": records, "evidence_refs": refs}}, 2000
    )
    assert admitted is not None
    output = support_observation(admitted, definition("search_knowledge"), 2000)
    assert output["data"]["results"] == records
    assert output["data"]["evidence_refs"] == refs


def test_frozen_version_keeps_legacy_prompt_and_rejects_unknown_policy():
    from copy import deepcopy

    from packages.agent_runtime.frozen import parse_frozen_agent_spec
    from packages.core.canonical.json_hash import canonical_json_hash
    from tests.unit.test_agent_publish import _agent, _spec

    # Keep the product freeze contract independent of archived business receipts.
    # Historical wire replay remains on the PR20 research branch.
    agent = _agent()
    original = _spec(agent)
    ws = agent.workspace_id
    original_hash = canonical_json_hash(original)
    old = parse_frozen_agent_spec(original, workspace_id=ws)
    assert "support_answer_policy" not in old.runtime
    assert support_system_prompt(old.system_prompt, old.runtime, []) == old.system_prompt
    candidate = deepcopy(original)
    candidate["runtime"]["support_answer_policy"] = "support_facts_v1"
    parsed = parse_frozen_agent_spec(candidate, workspace_id=ws)
    assert parsed.runtime["support_answer_policy"] == "support_facts_v1"
    assert canonical_json_hash(candidate) != original_hash
    assert canonical_json_hash(original) == original_hash
    candidate["runtime"]["support_answer_policy"] = "unsupported"
    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(candidate, workspace_id=ws)
