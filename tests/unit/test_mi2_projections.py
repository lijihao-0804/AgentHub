import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.schemas.feedback import FeedbackCreate, FeedbackImport
from packages.agent_runtime.events import AgentEvent
from packages.knowledge.contracts import RetrievalQuery, RetrievalResult, RetrievedEvidence
from packages.tools.projections import (
    argument_summary,
    evidence_refs,
    validate_evidence,
    validate_summary,
)


@pytest.mark.asyncio
async def test_fixed_refs_stay_with_ranked_hit_when_chunk_ids_repeat(monkeypatch):
    module = import_module("packages.tools.builtins.search_knowledge")
    queries = [
        RetrievalQuery("q", str(uuid4()), str(uuid4())),
        RetrievalQuery("q", str(uuid4()), str(uuid4())),
    ]

    async def snapshot_queries(*args, **kwargs):
        return queries

    @asynccontextmanager
    async def factory():
        yield None

    class Retriever:
        async def retrieve_with_trace(self, context, query):
            hit = RetrievedEvidence(
                str(uuid4()),
                str(uuid4()),
                "same-chunk",
                "fixture",
                {},
                "text",
                2.0 if query is queries[0] else 1.0,
                None,
                {},
            )
            return RetrievalResult((hit,), None)

    monkeypatch.setattr(module, "_snapshot_queries", snapshot_queries)
    monkeypatch.setattr(module, "_retrieval_context", lambda context: None)
    result = await module.search_knowledge(
        None, None, {"query": "q", "limit": 1}, factory, retriever=Retriever()
    )
    assert result["evidence_refs"][0]["snapshot_id"] == queries[0].knowledge_snapshot_id
    assert (
        result["evidence_refs"][0]["document_revision_id"]
        == (result["results"][0]["document_revision_id"])
    )


def event(payload):
    return AgentEvent(
        sequence=1,
        type="tool.requested",
        request_id="req",
        run_id="run",
        agent_version_id="v",
        timestamp=datetime.now(UTC),
        payload=payload,
    )


def test_summary_has_fixed_names_and_types_only():
    args = {
        "query": "SECRET_EMAIL@example.test",
        "customer_id": "PRIVATE-ID",
        "password": "s",
        "api_key": "s",
        "unknown-PII": {"secret": "NESTED"},
        "description": ["private"],
    }
    summary = argument_summary(args)
    assert "SECRET" not in summary and "PRIVATE" not in summary and "NESTED" not in summary
    assert "password" not in summary and "api_key" not in summary and "unknown-PII" not in summary
    assert json.loads(summary)["hidden_count"] == 3
    assert validate_summary(summary) == summary
    event({"tool_identity": "search", "arguments_summary": summary})
    oversized = argument_summary({f"hidden-{i}": "secret" for i in range(10001)})
    assert json.loads(oversized)["hidden_count"] == 10000
    assert validate_summary(oversized) == oversized


@pytest.mark.parametrize(
    "value",
    [
        '{"fields":{"query":"secret value"},"hidden_count":0}',
        '{"fields":{"password":"string"},"hidden_count":0}',
        '{"fields":{"query":{"secret":"x"}},"hidden_count":0}',
        '{"fields":{},"hidden_count":true}',
        "x" * 2049,
    ],
)
def test_unsafe_summary_cannot_enter_events(value):
    with pytest.raises(ValueError):
        event({"tool_identity": "t", "arguments_summary": value})


def test_bounded_evidence_identity_without_content():
    ref = {
        "snapshot_id": str(uuid4()),
        "knowledge_base_id": str(uuid4()),
        "document_revision_id": str(uuid4()),
        "chunk_id": "chunk-safe",
    }
    assert evidence_refs({"evidence_refs": [ref]}) == (ref,)
    assert validate_evidence(json.dumps([ref]))
    with pytest.raises(ValueError):
        validate_evidence(json.dumps([{**ref, "text": "PRIVATE"}]))
    assert evidence_refs({"evidence_refs": [{**ref, "chunk_id": "name@example.test"}]}) == ()


def test_feedback_strict_api_contract_and_no_holdout_input():
    with pytest.raises(ValidationError):
        FeedbackCreate(client_key="k", rating=0, category="OTHER")
    with pytest.raises(ValidationError):
        FeedbackImport(
            dataset_id=uuid4(), base_version_id=uuid4(), expected_review_version=2, split="HOLDOUT"
        )
    with pytest.raises(ValidationError):
        FeedbackCreate(client_key="k", rating=-1, category="OTHER", corrected_answer="x" * 16001)
