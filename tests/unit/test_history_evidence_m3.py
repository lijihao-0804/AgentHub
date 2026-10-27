"""M3 pure contract for restoring compact RAG evidence from history."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from packages.agent_runtime.history_evidence import (
    FORBIDDEN,
    HISTORY_EVIDENCE_INVALID,
    validate_history_evidence,
)
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from packages.wiki.evidence_envelope import envelope_violation, wiki_evidence_reference


def _valid_case() -> dict:
    knowledge_base_id = str(uuid4())
    snapshot_id = str(uuid4())
    raw_revision_id = str(uuid4())
    wiki_revision_id = str(uuid4())
    publication_id = str(uuid4())
    snapshot_hash = "a" * 64
    publication_hash = "b" * 64

    raw_record = {
        "origin": "RAW",
        "knowledge_base_id": knowledge_base_id,
        "snapshot_id": snapshot_id,
        "document_revision_id": raw_revision_id,
        "chunk_id": "chunk-raw",
        "document_id": "document-raw",
        "snippet": "raw evidence",
    }
    raw_reference = {
        "knowledge_base_id": knowledge_base_id,
        "snapshot_id": snapshot_id,
        "document_revision_id": raw_revision_id,
        "chunk_id": "chunk-raw",
    }
    raw_plan = assemble_evidence_plan(
        [raw_record],
        [raw_reference],
        budget=64_000,
        estimator=lambda text: len(text.encode("utf-8")),
        citation_prefix="H1E",
    )
    assert raw_plan is not None

    wiki_record = {
        "origin": "WIKI_DERIVED",
        "publication_id": publication_id,
        "publication_hash": publication_hash,
        "wiki_page_id": "page-1",
        "source_revisions": [wiki_revision_id],
        "quote": "wiki evidence",
        "snippet": "wiki evidence",
        "knowledge_base_id": knowledge_base_id,
        "snapshot_id": snapshot_id,
        "document_revision_id": wiki_revision_id,
        "chunk_id": "chunk-wiki",
    }
    wiki_reference = wiki_evidence_reference(
        wiki_record,
        snapshot_id=snapshot_id,
        knowledge_base_id=knowledge_base_id,
    )
    wiki_plan = assemble_evidence_plan(
        [wiki_record],
        [wiki_reference],
        budget=64_000,
        estimator=lambda text: len(text.encode("utf-8")),
        citation_prefix="H2E",
        record_validator=envelope_violation,
        allow_shared_references=True,
    )
    assert wiki_plan is not None

    snapshots = [
        {
            "knowledge_base_id": knowledge_base_id,
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
        }
    ]
    wiki = [
        {
            "knowledge_base_id": knowledge_base_id,
            "binding_mode": "PINNED",
            "wiki_publication_id": publication_id,
            "publication_hash": publication_hash,
        }
    ]
    return {
        "messages": (
            ModelMessage("user", "question"),
            ModelMessage(
                "assistant",
                tool_calls=(
                    ModelToolCall("search_knowledge", {}, "call-raw"),
                    ModelToolCall("query_wiki", {}, "call-wiki"),
                ),
            ),
            ModelMessage(
                "tool",
                json.dumps(raw_plan.payload, ensure_ascii=False),
                name="search_knowledge",
                tool_call_id="call-raw",
            ),
            ModelMessage(
                "tool",
                json.dumps(wiki_plan.payload, ensure_ascii=False),
                name="query_wiki",
                tool_call_id="call-wiki",
            ),
            ModelMessage("assistant", "answer"),
        ),
        "registry": {**raw_plan.registry, **wiki_plan.registry},
        "source_snapshots": snapshots,
        "current_snapshots": copy.deepcopy(snapshots),
        "source_wiki": wiki,
        "current_wiki": copy.deepcopy(wiki),
        "permissions": frozenset({"knowledge_run", "tool_run"}),
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
    arguments.update(updates)
    return validate_history_evidence(**arguments)


def _assert_invalid(case: dict, **updates) -> None:
    with pytest.raises(AgentHubError) as raised:
        _validate(case, **updates)
    assert raised.value.code == HISTORY_EVIDENCE_INVALID
    assert raised.value.message == "Historical RAG evidence is unavailable."
    assert raised.value.status_code == 422


def test_valid_raw_and_wiki_registry_is_returned_as_a_detached_copy():
    case = _valid_case()

    verified = _validate(case)

    assert verified == case["registry"]
    assert set(verified) == {"H1E1", "H2E1"}
    verified["H1E1"]["record"]["snippet"] = "changed"
    assert case["registry"]["H1E1"]["record"]["snippet"] == "raw evidence"


def test_a_visible_compact_record_without_its_registry_entry_fails_closed():
    case = _valid_case()
    case["registry"].pop("H2E1")

    _assert_invalid(case)


def test_record_and_reference_identity_mismatch_fails_closed():
    case = _valid_case()
    case["registry"]["H1E1"]["reference"]["document_revision_id"] = str(uuid4())

    _assert_invalid(case)


@pytest.mark.parametrize(
    "permissions",
    [
        frozenset({"knowledge_run"}),
        frozenset({"tool_run"}),
        frozenset(),
    ],
)
def test_either_revoked_history_permission_fails_closed(permissions):
    case = _valid_case()

    with pytest.raises(AgentHubError) as raised:
        _validate(case, permissions=permissions)
    assert raised.value.code == FORBIDDEN
    assert raised.value.message == "Historical RAG evidence is not authorized."
    assert raised.value.status_code == 403


def test_a_different_current_snapshot_cannot_reuse_historical_evidence():
    case = _valid_case()
    current = copy.deepcopy(case["current_snapshots"])
    current[0]["snapshot_id"] = str(uuid4())
    current[0]["snapshot_hash"] = "c" * 64

    _assert_invalid(case, current_snapshots=current)


@pytest.mark.parametrize("field", ["wiki_publication_id", "publication_hash"])
def test_wiki_publication_identity_must_match_on_both_sides(field):
    case = _valid_case()
    current = copy.deepcopy(case["current_wiki"])
    current[0][field] = str(uuid4()) if field == "wiki_publication_id" else "c" * 64

    _assert_invalid(case, current_wiki=current)


def test_successful_noncompact_rag_history_is_unsupported():
    case = _valid_case()
    legacy = {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": False,
        "data": {"results": [], "evidence_refs": []},
    }
    messages = list(case["messages"])
    messages[2] = replace(messages[2], content=json.dumps(legacy))

    _assert_invalid(case, messages=messages)
