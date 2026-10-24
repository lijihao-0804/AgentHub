"""P-E0: version-level opt-in for Onyx source-section selection."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from packages.agent_runtime.publish import _validate_retrieval_config
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.adapters.onyx_selection import (
    OpenAICompatibleSectionSelector,
    SectionSelectorError,
    promote_evidence,
    sections_from_evidence,
)
from packages.knowledge.contracts import RetrievalQuery, RetrievedEvidence
from packages.knowledge.onyx_retriever import OnyxSelectionKnowledgeRetriever


def _evidence(
    chunk_id: str,
    document_id: str,
    *,
    ordinal: int,
    text: str = "passage",
    title: str | None = "doc",
) -> RetrievedEvidence:
    return RetrievedEvidence(
        document_id=document_id,
        document_revision_id=f"rev-{document_id}",
        chunk_id=chunk_id,
        source="fixture",
        locator={"type": "text_range", "char_start": ordinal * 100, "char_end": ordinal * 100 + 50},
        text=text,
        retrieval_score=1.0 - ordinal * 0.01,
        rerank_score=1.0 - ordinal * 0.01,
        metadata={"ordinal": ordinal, "document_name": title},
    )


def _pool() -> tuple[RetrievedEvidence, ...]:
    return (
        _evidence("a1", "doc-a", ordinal=0),
        _evidence("a2", "doc-a", ordinal=1),
        _evidence("b1", "doc-b", ordinal=2),
        _evidence("b2", "doc-b", ordinal=3),
        _evidence("c1", "doc-c", ordinal=4),
        _evidence("c2", "doc-c", ordinal=5),
    )


class _FakeRetriever:
    def __init__(self, evidence: tuple[RetrievedEvidence, ...]) -> None:
        self.evidence = evidence
        self.queries: list[RetrievalQuery] = []

    async def retrieve_with_trace(self, context, query):
        from packages.knowledge.contracts import (
            RetrievalResult,
            RetrievalTrace,
            RetrievalTraceStage,
        )

        self.queries.append(query)
        empty = RetrievalTraceStage(0.0, ())
        return RetrievalResult(
            evidence=self.evidence,
            trace=RetrievalTrace(
                snapshot_id=query.knowledge_snapshot_id,
                dense=empty,
                sparse=empty,
                fusion=empty,
                rerank=empty,
                total_latency_ms=0.0,
            ),
        )


class _Selector:
    def __init__(self, ids: tuple[int, ...] | Exception) -> None:
        self.ids = ids
        self.calls: list[tuple[str, int]] = []

    async def select_sections(self, context, query, sections) -> tuple[int, ...]:
        if isinstance(self.ids, Exception):
            raise self.ids
        self.calls.append((query, len(sections)))
        return self.ids


def _query(**overrides) -> RetrievalQuery:
    values = {
        "text": "question",
        "knowledge_base_id": str(uuid4()),
        "knowledge_snapshot_id": str(uuid4()),
        "candidate_top_k": 20,
        "final_top_k": 5,
        "context_budget_bytes": 64_000,
    }
    return RetrievalQuery(**{**values, **overrides})


# ---------------------------------------------------------------------------
# E0-1: publication contract
# ---------------------------------------------------------------------------


def test_publish_accepts_valid_onyx_profile() -> None:
    result = _validate_retrieval_config(
        {
            "context_budget_bytes": 8_000,
            "evidence_set_selector": "onyx_v1",
        }
    )
    assert result["evidence_set_selector"] == "onyx_v1"


def test_publish_rejects_onyx_without_budget_or_with_projection_or_oversized() -> None:
    with pytest.raises(AgentHubError):
        _validate_retrieval_config({"evidence_set_selector": "onyx_v1"})
    with pytest.raises(AgentHubError):
        _validate_retrieval_config(
            {
                "context_budget_bytes": 8_000,
                "evidence_projection": "compact_v1",
                "evidence_set_selector": "onyx_v1",
            }
        )
    with pytest.raises(AgentHubError):
        _validate_retrieval_config(
            {
                "context_budget_bytes": 8_000,
                "final_top_k": 11,
                "evidence_set_selector": "onyx_v1",
            }
        )
    with pytest.raises(AgentHubError):
        _validate_retrieval_config(
            {"context_budget_bytes": 8_000, "evidence_set_selector": "onyx_v2"}
        )


def test_publish_omits_selector_key_when_not_opted_in() -> None:
    result = _validate_retrieval_config({"context_budget_bytes": 8_000})
    assert "evidence_set_selector" not in result


# ---------------------------------------------------------------------------
# E0-2: sections and promotion from evidence
# ---------------------------------------------------------------------------


def test_sections_group_by_source_cap_three_children_and_order_by_ordinal() -> None:
    pool = (
        _evidence("a1", "doc-a", ordinal=2),
        _evidence("a2", "doc-a", ordinal=0),
        _evidence("a3", "doc-a", ordinal=1),
        _evidence("a4", "doc-a", ordinal=3),
        _evidence("b1", "doc-b", ordinal=4, title=None),
    )
    sections = sections_from_evidence(pool)

    assert [section["source_id"] for section in sections] == ["doc-a", "doc-b"]
    first = sections[0]
    assert first["chunk_ids"] == ["a2", "a3", "a1"]  # ordinal order, capped at 3
    assert first["title"] == "doc"
    assert sections[1]["title"] == ""


def test_promote_evidence_moves_selected_sources_first_and_keeps_every_item() -> None:
    pool = _pool()
    sections = sections_from_evidence(pool)

    promoted = promote_evidence(pool, sections, (2, 0))

    assert [item.document_id for item in promoted] == [
        "doc-c",
        "doc-c",
        "doc-a",
        "doc-a",
        "doc-b",
        "doc-b",
    ]
    assert promote_evidence(pool, sections, ()) == pool


# ---------------------------------------------------------------------------
# E0-3: wrapper success and fail-closed fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_promotes_selected_sections_and_marks_trace() -> None:
    pool = _pool()
    inner = _FakeRetriever(pool)
    selector = _Selector((2,))
    wrapper = OnyxSelectionKnowledgeRetriever(inner, selector)

    result = await wrapper.retrieve_with_trace(object(), _query())

    assert selector.calls and selector.calls[0][1] == 3  # three sections
    assert [item.document_id for item in result.evidence] == [
        "doc-c",
        "doc-c",
        "doc-a",
        "doc-a",
        "doc-b",
    ][: len(result.evidence)]
    stage = result.trace.evidence_set
    assert stage is not None
    assert stage.metadata["selector"] == "onyx_v1"
    assert stage.metadata["status"] == "selected"
    assert stage.metadata["selected_sections"] == 1
    # The pool call expanded the inner cut to the candidate budget.
    assert inner.queries[0].final_top_k == 20
    assert inner.queries[0].context_budget_bytes is None


@pytest.mark.asyncio
async def test_wrapper_unconfigured_selector_falls_back_identically() -> None:
    pool = _pool()
    inner = _FakeRetriever(pool)

    wrapper = OnyxSelectionKnowledgeRetriever(inner, None)
    fallback = await wrapper.retrieve_with_trace(object(), _query())
    direct = await inner.retrieve_with_trace(object(), _query())

    assert [item.chunk_id for item in fallback.evidence] == [
        item.chunk_id for item in direct.evidence
    ][: len(fallback.evidence)]
    stage = fallback.trace.evidence_set
    assert stage is not None
    assert stage.metadata["status"] == "fallback"
    assert stage.metadata["reason"] == "unconfigured"


@pytest.mark.asyncio
async def test_wrapper_selector_failure_falls_back_with_reason() -> None:
    inner = _FakeRetriever(_pool())
    selector = _Selector(SectionSelectorError("gateway down"))
    wrapper = OnyxSelectionKnowledgeRetriever(inner, selector)

    result = await wrapper.retrieve_with_trace(object(), _query())

    stage = result.trace.evidence_set
    assert stage is not None
    assert stage.metadata["status"] == "fallback"
    assert stage.metadata["reason"].startswith("selector_failed")
    assert result.evidence  # original ranking still delivered


@pytest.mark.asyncio
async def test_wrapper_invalid_selection_ids_fall_back() -> None:
    inner = _FakeRetriever(_pool())
    selector = _Selector((99,))  # out of range for 3 sections
    wrapper = OnyxSelectionKnowledgeRetriever(inner, selector)

    result = await wrapper.retrieve_with_trace(object(), _query())

    stage = result.trace.evidence_set
    assert stage is not None
    assert stage.metadata["status"] == "fallback"
    assert stage.metadata["reason"].startswith("invalid_selection")


@pytest.mark.asyncio
async def test_wrapper_rejects_out_of_profile_queries() -> None:
    inner = _FakeRetriever(_pool())
    wrapper = OnyxSelectionKnowledgeRetriever(inner, _Selector(()))

    for bad in (
        {"final_top_k": 11},
        {"candidate_top_k": 21},
        {"context_budget_bytes": None},
        {"evidence_projection": "compact_v1"},
    ):
        with pytest.raises(ValueError):
            await wrapper.retrieve_with_trace(object(), _query(**bad))


@pytest.mark.asyncio
async def test_search_knowledge_routes_flag_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from importlib import import_module

    module = import_module("packages.tools.builtins.search_knowledge")

    async def snapshot_queries(_session, _context, definition, *, text, limit):
        return [
            RetrievalQuery(
                text=text,
                knowledge_base_id=str(uuid4()),
                knowledge_snapshot_id=str(uuid4()),
                final_top_k=limit,
                candidate_top_k=20,
                context_budget_bytes=8_000,
            )
        ]

    @asynccontextmanager
    async def factory():
        yield None

    selector = _Selector((2,))  # section 2 = doc-c in first-hit order

    async def run(config: dict) -> tuple[list[str], int]:
        retriever = _FakeRetriever(_pool())
        definition = replace(
            _definition(config),
            retrieval_config={"knowledge_binding_mode": "PINNED", **config},
        )
        payload = await module.search_knowledge(
            _context(),
            definition,
            {"query": "q", "limit": 5},
            factory,
            retriever=retriever,
            section_selector=selector,
        )
        return (
            [record["chunk_id"] for record in payload["results"]],
            retriever.queries[0].final_top_k,
        )

    monkeypatch.setattr(module, "_snapshot_queries", snapshot_queries)
    monkeypatch.setattr(module, "_retrieval_context", lambda context: None)

    baseline_ids, baseline_pool = await run({"context_budget_bytes": 8_000})
    onyx_ids, onyx_pool = await run(
        {"context_budget_bytes": 8_000, "evidence_set_selector": "onyx_v1"}
    )

    # The tool re-sorts its union by score (production semantics), so the
    # selection changes WHICH records survive the cut, not tool-return order:
    # baseline drops the lowest-scored c2; the selection keeps all of doc-c
    # and drops b2 instead.
    assert baseline_ids == ["a1", "a2", "b1", "b2", "c1"]
    assert onyx_ids == ["a1", "a2", "b1", "c1", "c2"]  # score-sorted; c2 kept, b2 dropped
    assert baseline_pool == 5 and onyx_pool == 20  # wrapper widened the pool


def _definition(config: dict):
    from packages.tools.contracts import (
        ToolApprovalPolicy,
        ToolDefinition,
        ToolEffect,
        ToolRisk,
        ToolSourceKind,
    )

    return ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="spec-hash",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        retrieval_config=config,
        source_kind=ToolSourceKind.BUILTIN,
    )


def _context():
    from packages.core.execution_context.models import (
        OrganizationContext,
        PrincipalContext,
        WorkspaceExecutionContext,
    )

    return type(
        "Context",
        (),
        {
            "workspace_context": WorkspaceExecutionContext(
                organization=OrganizationContext(
                    principal=PrincipalContext(request_id="r", trace_id="t"),
                    organization_id=str(uuid4()),
                    org_role="OWNER",
                ),
                workspace_id=str(uuid4()),
                permissions=frozenset({"knowledge_run"}),
            ),
            "workspace_id": str(uuid4()),
            "agent_version_id": uuid4(),
            "tool_call_id": "call",
        },
    )()


def test_openai_selector_builds_single_attempt_client() -> None:
    selector = OpenAICompatibleSectionSelector(
        base_url="https://gateway.example/v1", api_key="k", model="m"
    )
    assert selector.model == "m"

    with pytest.raises(ValueError):
        OpenAICompatibleSectionSelector(base_url="", api_key="k", model="m")
