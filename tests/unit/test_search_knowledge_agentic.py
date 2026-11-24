"""Unit tests for Agentic RAG cross-turn dedup and targeted filters in search_knowledge."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRisk,
)
from packages.tools.errors import ToolHandlerError


class _FakeSession:
    def __init__(self, snapshots: dict[str, SimpleNamespace]) -> None:
        self._snapshots = snapshots

    async def scalar(self, stmt) -> SimpleNamespace | None:  # noqa: ANN001
        compiled = stmt.compile()
        for value in compiled.params.values():
            key = str(value)
            if key in self._snapshots:
                return self._snapshots[key]
        return None


class _FakeRetriever:
    def __init__(self, items_by_kb: dict[str, list[RetrievedEvidence]]) -> None:
        self._items_by_kb = items_by_kb

    async def retrieve_with_trace(
        self, context: WorkspaceExecutionContext, query: RetrievalQuery
    ) -> RetrievalResult:
        items = tuple(self._items_by_kb.get(query.knowledge_base_id, []))
        empty_stage = RetrievalTraceStage(latency_ms=1.0, results=())
        return RetrievalResult(
            evidence=items,
            trace=RetrievalTrace(
                snapshot_id=query.knowledge_snapshot_id,
                dense=empty_stage,
                sparse=empty_stage,
                fusion=empty_stage,
                rerank=empty_stage,
                total_latency_ms=4.0,
            ),
        )


def _evidence(chunk_id: str, title: str, text: str, score: float) -> RetrievedEvidence:
    return RetrievedEvidence(
        document_id=f"doc-{chunk_id}",
        document_revision_id=f"rev-{chunk_id}",
        chunk_id=chunk_id,
        source=title,
        locator={"page": 1},
        text=text,
        retrieval_score=score,
        rerank_score=score,
        metadata={"document_name": title},
    )


def _setup(
    *,
    seen_chunk_ids: frozenset[str] = frozenset(),
):
    ws_id = str(uuid4())
    org_id = str(uuid4())
    kb_a, kb_b = str(uuid4()), str(uuid4())
    snap_a, snap_b = str(uuid4()), str(uuid4())
    snapshots = {
        snap_a: SimpleNamespace(id=snap_a, knowledge_base_id=kb_a, content_hash="hash-a"),
        snap_b: SimpleNamespace(id=snap_b, knowledge_base_id=kb_b, content_hash="hash-b"),
    }

    @asynccontextmanager
    async def session_factory():
        yield _FakeSession(snapshots)

    ws_ctx = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="req-1",
                trace_id="trace-1",
                user_id=str(uuid4()),
            ),
            organization_id=org_id,
            org_role="member",
        ),
        workspace_id=ws_id,
        workspace_role="operator",
        permissions=frozenset({"tool_run", "knowledge_run"}),
    )
    tool_ctx = ToolExecutionContext(
        workspace_context=ws_ctx,
        agent_version_id=uuid4(),
        tool_call_id="call-1",
        seen_chunk_ids=seen_chunk_ids,
    )
    definition = ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="rev-hash",
        description="Search KB",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=10,
        retrieval_config={"knowledge_binding_mode": "PINNED"},
        snapshot_refs=(
            {"snapshot_id": snap_a, "snapshot_hash": "hash-a"},
            {"snapshot_id": snap_b, "snapshot_hash": "hash-b"},
        ),
    )
    retriever = _FakeRetriever(
        {
            kb_a: [
                _evidence("c1", "Refund Policy 2026", "Refunds within 15 days.", 0.95),
                _evidence("c2", "SLA Handbook", "Response within 2 hours.", 0.85),
            ],
            kb_b: [
                _evidence("c3", "Incident Runbook", "Escalate P0 within 5 min.", 0.90),
            ],
        }
    )
    return tool_ctx, definition, session_factory, retriever, kb_a, kb_b


@pytest.mark.asyncio
async def test_cross_turn_dedup_suppresses_seen_chunks_and_reports_count() -> None:
    ctx, definition, sf, retriever, _, _ = _setup(seen_chunk_ids=frozenset({"c1"}))
    out = await search_knowledge(ctx, definition, {"query": "refund"}, sf, retriever=retriever)
    returned_ids = [ref["chunk_id"] for ref in out["evidence_refs"]]
    assert returned_ids == ["c3", "c2"]
    assert out["already_seen_count"] == 1
    assert "all_results_already_seen" not in out


@pytest.mark.asyncio
async def test_cross_turn_dedup_returns_loop_break_hint_when_all_seen() -> None:
    ctx, definition, sf, retriever, _, _ = _setup(seen_chunk_ids=frozenset({"c1", "c2", "c3"}))
    out = await search_knowledge(ctx, definition, {"query": "refund"}, sf, retriever=retriever)
    assert out["results"] == []
    assert out["evidence_refs"] == []
    assert out["already_seen_count"] == 3
    assert out["all_results_already_seen"] is True
    assert "Rephrase" in out["hint"]


@pytest.mark.asyncio
async def test_document_title_and_knowledge_base_filters() -> None:
    ctx, definition, sf, retriever, kb_a, _ = _setup()
    out_kb = await search_knowledge(
        ctx,
        definition,
        {"query": "policy", "knowledge_base_id": kb_a},
        sf,
        retriever=retriever,
    )
    assert [ref["chunk_id"] for ref in out_kb["evidence_refs"]] == ["c1", "c2"]

    out_title = await search_knowledge(
        ctx,
        definition,
        {"query": "policy", "document_title": "refund"},
        sf,
        retriever=retriever,
    )
    assert [ref["chunk_id"] for ref in out_title["evidence_refs"]] == ["c1"]

    out_missing_kb = await search_knowledge(
        ctx,
        definition,
        {"query": "policy", "knowledge_base_id": str(uuid4())},
        sf,
        retriever=retriever,
    )
    assert out_missing_kb["results"] == []
    assert out_missing_kb["filter_matched_snapshots"] == 0


@pytest.mark.asyncio
async def test_explicit_exclude_chunk_ids_and_invalid_filters() -> None:
    ctx, definition, sf, retriever, _, _ = _setup()
    out = await search_knowledge(
        ctx,
        definition,
        {"query": "policy", "exclude_chunk_ids": ["c1", "c3"]},
        sf,
        retriever=retriever,
    )
    assert [ref["chunk_id"] for ref in out["evidence_refs"]] == ["c2"]
    assert out["already_seen_count"] == 2

    with pytest.raises(ToolHandlerError) as exc_info:
        await search_knowledge(
            ctx, definition, {"query": "policy", "document_title": "   "}, sf, retriever=retriever
        )
    assert exc_info.value.code == "SEARCH_INVALID_ARGUMENT"

    with pytest.raises(ToolHandlerError) as exc_info2:
        await search_knowledge(
            ctx,
            definition,
            {"query": "policy", "exclude_chunk_ids": ["ok", 123]},
            sf,
            retriever=retriever,
        )
    assert exc_info2.value.code == "SEARCH_INVALID_ARGUMENT"
