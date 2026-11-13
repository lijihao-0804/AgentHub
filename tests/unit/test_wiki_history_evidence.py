"""Strict compact-envelope support for model-visible Wiki history."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.history_evidence import (
    HISTORY_EVIDENCE_INVALID,
    validate_history_evidence,
)
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_runtime.wiki_history_evidence import (
    _matching_claim,
    _ResolvedPublication,
    _verify_source_revisions,
)
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.wiki.evidence_envelope import (
    envelope_violation,
    stamp_wiki_derived_evidence,
    wiki_evidence_reference,
)


def _case() -> dict:
    knowledge_base_id = str(uuid4())
    snapshot_id = str(uuid4())
    publication_id = str(uuid4())
    document_id = str(uuid4())
    revision_id = str(uuid4())
    publication_hash = "b" * 64
    snapshot_hash = "a" * 64
    page = {
        "page_key": "retention",
        "title": "Retention",
        "kind": "concept",
        "markdown": "# Retention\n\nRetention period is 7 years.",
        "claims": [
            {
                "page_key": "retention",
                "chunk_id": "chunk-retention",
                "document_id": document_id,
                "document_revision_id": revision_id,
                "locator": {"type": "text_range", "char_start": 0, "char_end": 27},
                "quote": "Retention period is 7 years.",
                "conflict": False,
            }
        ],
        "relations": [],
    }
    record = stamp_wiki_derived_evidence(
        page["claims"],
        publication_id=publication_id,
        publication_hash=publication_hash,
    )[0]
    record["snippet"] = record["quote"]
    reference = wiki_evidence_reference(
        record,
        snapshot_id=snapshot_id,
        knowledge_base_id=knowledge_base_id,
    )
    plan = assemble_evidence_plan(
        [record],
        [reference],
        budget=8_000,
        estimator=len,
        citation_prefix="W001E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert plan is not None
    binding = {
        "knowledge_base_id": knowledge_base_id,
        "binding_mode": "LATEST",
        "wiki_publication_id": publication_id,
        "publication_hash": publication_hash,
    }
    resolved = {
        "knowledge_base_id": knowledge_base_id,
        "wiki_publication_id": publication_id,
        "publication_hash": publication_hash,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
    }
    messages = (
        ModelMessage("user", "What is the retention period?"),
        ModelMessage(
            "assistant",
            tool_calls=(ModelToolCall("query_wiki", {"query": "retention"}, "call-wiki"),),
        ),
        ModelMessage(
            "tool",
            json.dumps(plan.payload, ensure_ascii=False),
            name="query_wiki",
            tool_call_id="call-wiki",
        ),
        ModelMessage("assistant", "The retention period is 7 years [W001E1]."),
    )
    publication = _ResolvedPublication(
        knowledge_base_id=UUID(knowledge_base_id),
        publication_id=UUID(publication_id),
        publication_hash=publication_hash,
        snapshot_id=UUID(snapshot_id),
        snapshot_hash=snapshot_hash,
        pages=(page,),
        source_revision_ids=frozenset({UUID(revision_id)}),
    )
    return {
        "messages": messages,
        "registry": plan.registry,
        "source_snapshots": (),
        "current_snapshots": (),
        "source_wiki": (binding,),
        "current_wiki": (copy.deepcopy(binding),),
        "permissions": frozenset({"knowledge_run", "tool_run"}),
        "resolved_wiki_snapshots": (resolved,),
        "page": page,
        "record": record,
        "reference": reference,
        "resolved_publication": publication,
    }


def _validate(case: dict, **updates):
    arguments = {
        key: case[key]
        for key in (
            "messages",
            "registry",
            "source_snapshots",
            "current_snapshots",
            "source_wiki",
            "current_wiki",
            "permissions",
        )
    }
    arguments["resolved_wiki_snapshots"] = updates.pop(
        "resolved_wiki_snapshots", case["resolved_wiki_snapshots"]
    )
    arguments.update(updates)
    return validate_history_evidence(**arguments)


def _assert_invalid(case: dict, **updates) -> None:
    with pytest.raises(AgentHubError) as raised:
        _validate(case, **updates)
    assert raised.value.code == HISTORY_EVIDENCE_INVALID
    assert raised.value.status_code == 422


def test_wiki_only_agent_recovers_exact_visible_registry_without_raw_bindings():
    case = _case()

    verified = _validate(case)

    assert case["source_snapshots"] == ()
    assert case["current_snapshots"] == ()
    assert verified == case["registry"]
    verified["W001E1"]["record"]["snippet"] = "changed"
    assert case["registry"]["W001E1"]["record"]["snippet"] == "Retention period is 7 years."


def test_database_resolved_snapshot_is_required_for_wiki_only_history():
    case = _case()

    _assert_invalid(case, resolved_wiki_snapshots=())


def test_historical_reference_cannot_nominate_a_different_snapshot():
    case = _case()
    reference = dict(case["reference"], snapshot_id=str(uuid4()))
    registry = copy.deepcopy(case["registry"])
    registry["W001E1"]["reference"] = reference

    _assert_invalid(case, registry=registry)


@pytest.mark.parametrize("side", ["source_wiki", "current_wiki"])
def test_source_and_current_publication_identity_must_match(side):
    case = _case()
    binding = copy.deepcopy(case[side])
    binding[0]["publication_hash"] = "c" * 64

    _assert_invalid(case, **{side: binding})


def test_database_resolved_publication_hash_must_match_both_runs():
    case = _case()
    resolved = copy.deepcopy(case["resolved_wiki_snapshots"])
    resolved[0]["publication_hash"] = "c" * 64

    _assert_invalid(case, resolved_wiki_snapshots=resolved)


def test_database_resolved_snapshot_cannot_authorize_raw_evidence():
    case = _case()
    record = {
        "origin": "RAW",
        "knowledge_base_id": case["reference"]["knowledge_base_id"],
        "snapshot_id": case["resolved_wiki_snapshots"][0]["snapshot_id"],
        "document_revision_id": case["reference"]["document_revision_id"],
        "chunk_id": case["reference"]["chunk_id"],
        "document_id": case["record"]["document_id"],
        "snippet": case["record"]["snippet"],
    }
    reference = {
        key: case["reference"][key]
        for key in ("knowledge_base_id", "snapshot_id", "document_revision_id", "chunk_id")
    }
    plan = assemble_evidence_plan(
        [record],
        [reference],
        budget=8_000,
        estimator=len,
        citation_prefix="W001E",
    )
    assert plan is not None
    messages = (
        ModelMessage(
            "assistant",
            tool_calls=(ModelToolCall("search_knowledge", {}, "call-raw"),),
        ),
        ModelMessage(
            "tool",
            json.dumps(plan.payload, ensure_ascii=False),
            name="search_knowledge",
            tool_call_id="call-raw",
        ),
    )

    _assert_invalid(case, messages=messages, registry=plan.registry)


def test_search_knowledge_cannot_claim_wiki_derived_origin():
    case = _case()
    messages = list(case["messages"])
    messages[1] = replace(
        messages[1],
        tool_calls=(ModelToolCall("search_knowledge", {}, "call-wiki"),),
    )
    messages[2] = replace(messages[2], name="search_knowledge")

    _assert_invalid(case, messages=tuple(messages))


@pytest.mark.parametrize("failure", ["wrong_tool", "legacy_payload"])
def test_wrong_tool_pair_or_legacy_payload_fails_closed(failure):
    case = _case()
    messages = list(case["messages"])
    if failure == "wrong_tool":
        messages[2] = replace(messages[2], name="search_knowledge")
    else:
        legacy = {
            "evidence_format": "rag_records_v1",
            "status": "SUCCESS",
            "trust": "UNTRUSTED",
            "truncated": False,
            "data": {"results": [], "evidence_refs": []},
        }
        messages[2] = replace(messages[2], content=json.dumps(legacy))

    _assert_invalid(case, messages=tuple(messages))


def test_missing_visible_registry_entry_is_not_repaired():
    case = _case()

    _assert_invalid(case, registry={})


@pytest.mark.asyncio
async def test_malformed_registry_uses_history_context_gate_instead_of_attribute_error():
    workspace_id = uuid4()
    graph = _AgentRunGraph.__new__(_AgentRunGraph)
    graph.context = SimpleNamespace(
        workspace_id=str(workspace_id),
        permissions=frozenset({"knowledge_run", "tool_run"}),
        user_id="user-1",
    )
    graph.run = SimpleNamespace(
        effective_knowledge_snapshots=[],
        effective_wiki_publications=[],
    )
    graph.service = SimpleNamespace()
    turn = SimpleNamespace(
        messages=(
            ModelMessage(
                "assistant",
                tool_calls=(ModelToolCall("search_knowledge", {}, "call-raw"),),
            ),
        ),
        evidence_registry=[{"malformed": True}],
        source_knowledge_snapshots=(),
        source_wiki_publications=(),
    )

    with pytest.raises(AgentHubError) as rejected:
        await graph._history_evidence_registry(
            SimpleNamespace(turns=(turn,)),
            workspace_id=workspace_id,
        )

    assert rejected.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    assert rejected.value.status_code == 422


def test_incomplete_envelope_is_rejected_even_when_payload_is_truncated():
    case = _case()
    registry = copy.deepcopy(case["registry"])
    del registry["W001E1"]["record"]["publication_hash"]
    payload = json.loads(case["messages"][2].content)
    payload["truncated"] = True
    payload["degraded"] = True
    payload["degraded_reason"] = "EVIDENCE_TRUNCATED"
    payload["status"] = "DEGRADED"
    messages = list(case["messages"])
    messages[2] = replace(messages[2], content=json.dumps(payload))

    _assert_invalid(case, messages=tuple(messages), registry=registry)


def test_claim_verification_rejects_quote_and_locator_tampering():
    case = _case()
    publication = case["resolved_publication"]
    reference = case["reference"]

    with pytest.raises(AgentHubError) as quote_error:
        _matching_claim(
            publication,
            {**case["record"], "quote": "A different quote", "snippet": "A different quote"},
            reference,
            case["page"],
        )
    assert quote_error.value.code == HISTORY_EVIDENCE_INVALID

    tampered_reference = {**reference, "chunk_id": "another-chunk"}
    with pytest.raises(AgentHubError) as locator_error:
        _matching_claim(
            publication,
            case["record"],
            tampered_reference,
            case["page"],
        )
    assert locator_error.value.code == HISTORY_EVIDENCE_INVALID


@pytest.mark.parametrize(
    "failure",
    ["missing", "duplicate", "foreign", "invalid"],
)
def test_page_source_revision_set_must_be_complete_and_canonical(failure):
    case = _case()
    revision_id = case["reference"]["document_revision_id"]
    source_revisions = {
        "missing": [],
        "duplicate": [revision_id, revision_id],
        "foreign": [str(uuid4())],
        "invalid": ["not-a-uuid"],
    }[failure]
    record = {**case["record"], "source_revisions": source_revisions}

    with pytest.raises(AgentHubError) as error:
        _verify_source_revisions(record, case["page"])
    assert error.value.code == HISTORY_EVIDENCE_INVALID
