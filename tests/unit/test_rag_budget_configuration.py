from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.models import Agent
from packages.agent_runtime.publish import (
    DEFAULT_RETRIEVAL_CONFIG,
    _resolved_spec,
    _validate_retrieval_config,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.evaluation.observations import retrieval_observation
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from packages.model_gateway.contracts import ModelCapabilities
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.tools.builtins.search_knowledge import _snapshot_queries
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRisk,
)


def test_explicit_retrieval_budgets_are_validated_and_preserved() -> None:
    configured = _validate_retrieval_config(
        {
            "candidate_top_k": 100,
            "rerank_top_k": 100,
            "max_chunks_per_document": 1,
        }
    )

    assert configured["rerank_top_k"] == 100
    assert configured["max_chunks_per_document"] == 1

    explicit_none = _validate_retrieval_config(
        {"rerank_top_k": None, "max_chunks_per_document": None}
    )
    assert "rerank_top_k" in explicit_none
    assert "max_chunks_per_document" in explicit_none
    assert explicit_none["rerank_top_k"] is None
    assert explicit_none["max_chunks_per_document"] is None


@pytest.mark.parametrize("key", ["rerank_top_k", "max_chunks_per_document"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 101, 1.0, "2"])
def test_retrieval_budget_fields_reject_non_integer_or_out_of_range_values(
    key: str, value: object
) -> None:
    with pytest.raises(AgentHubError) as raised:
        _validate_retrieval_config({key: value})

    assert raised.value.code == "INVALID_AGENT_CONFIG"


def test_rerank_budget_cannot_exceed_candidate_pool() -> None:
    with pytest.raises(AgentHubError) as raised:
        _validate_retrieval_config({"candidate_top_k": 5, "rerank_top_k": 6})

    assert raised.value.code == "INVALID_AGENT_CONFIG"


def test_legacy_resolved_snapshot_does_not_gain_optional_budget_keys() -> None:
    agent = _agent(retrieval_config={})
    profile = _profile(agent)

    resolved = _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "offline"}),
        tools=(),
    )

    assert resolved["retrieval"]["candidate_top_k"] == DEFAULT_RETRIEVAL_CONFIG["candidate_top_k"]
    assert "rerank_top_k" not in resolved["retrieval"]
    assert "max_chunks_per_document" not in resolved["retrieval"]


def test_explicit_resolved_snapshot_preserves_new_budget_keys() -> None:
    agent = _agent(
        retrieval_config={
            "candidate_top_k": 60,
            "rerank_top_k": 20,
            "max_chunks_per_document": 2,
        }
    )
    profile = _profile(agent)

    resolved = _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "offline"}),
        tools=(),
    )

    assert resolved["retrieval"]["candidate_top_k"] == 60
    assert resolved["retrieval"]["rerank_top_k"] == 20
    assert resolved["retrieval"]["max_chunks_per_document"] == 2


def test_legacy_mapping_with_only_old_keys_keeps_optional_budgets_absent() -> None:
    legacy = _LegacyMapping({"candidate_top_k": 40, "final_top_k": 5})

    configured = _validate_retrieval_config(legacy)

    assert configured["candidate_top_k"] == 40
    assert "rerank_top_k" not in configured
    assert "max_chunks_per_document" not in configured


@pytest.mark.asyncio
async def test_search_knowledge_query_forwards_explicit_candidate_budgets() -> None:
    snapshot_id = uuid4()
    query = (
        await _snapshot_queries(
            _SnapshotSession(snapshot_id),
            _tool_context(),
            _definition(
                snapshot_id,
                {
                    "knowledge_binding_mode": "PINNED",
                    "candidate_top_k": 60,
                    "rerank_top_k": 20,
                    "max_chunks_per_document": 2,
                },
            ),
            text="question",
            limit=5,
        )
    )[0]

    assert query.candidate_top_k == 60
    assert query.rerank_top_k == 20
    assert query.max_chunks_per_document == 2


@pytest.mark.asyncio
async def test_search_knowledge_query_keeps_legacy_budget_defaults() -> None:
    snapshot_id = uuid4()
    query = (
        await _snapshot_queries(
            _SnapshotSession(snapshot_id),
            _tool_context(),
            _definition(snapshot_id, {"knowledge_binding_mode": "PINNED"}),
            text="question",
            limit=5,
        )
    )[0]

    assert query.candidate_top_k == DEFAULT_RETRIEVAL_CONFIG["candidate_top_k"]
    assert query.rerank_top_k is None
    assert query.max_chunks_per_document is None


def test_retrieval_observation_exposes_actual_selection_without_changing_candidates() -> None:
    query = RetrievalQuery(
        "question",
        "kb",
        "snapshot",
        candidate_top_k=3,
        final_top_k=2,
        rerank_top_k=2,
        max_chunks_per_document=1,
    )
    trace = _trace(
        fusion=("candidate-a", "candidate-b", "candidate-c"),
        selection=("candidate-a", "candidate-b"),
        rerank=("candidate-b", "candidate-a"),
    )
    result = RetrievalResult(
        evidence=(
            _evidence("candidate-b"),
            _evidence("candidate-a"),
        ),
        trace=trace,
    )

    observation = retrieval_observation(result, query, variant_hash="variant")

    assert observation["candidate_chunk_ids"] == [
        "candidate-a",
        "candidate-b",
        "candidate-c",
    ]
    assert observation["selection_chunk_ids"] == ["candidate-a", "candidate-b"]
    assert observation["rerank_top_k"] == 2
    assert observation["max_chunks_per_document"] == 1
    assert observation["timing"]["stage_durations_ms"]["selection"] == [1.0]
    assert observation["field_sources"]["selection_chunk_ids"] == "retrieval_trace.selection"


def test_retrieval_observation_accepts_legacy_trace_without_selection() -> None:
    query = RetrievalQuery("question", "kb", "snapshot")
    result = RetrievalResult(
        evidence=(_evidence("final"),),
        trace=_trace(fusion=("candidate",), rerank=("final",)),
    )

    observation = retrieval_observation(result, query, variant_hash="variant")

    assert observation["candidate_chunk_ids"] == ["candidate"]
    assert observation["selection_chunk_ids"] is None
    assert "selection" not in observation["timing"]["stage_durations_ms"]
    assert "selection_chunk_ids" not in observation["field_sources"]


class _SnapshotSession:
    def __init__(self, snapshot_id: UUID) -> None:
        self.snapshot_id = snapshot_id

    async def scalar(self, _statement):
        return SimpleNamespace(
            id=self.snapshot_id,
            knowledge_base_id=uuid4(),
            content_hash="a" * 64,
        )


class _LegacyMapping(Mapping[str, Any]):
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _tool_context() -> ToolExecutionContext:
    workspace_context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        permissions=frozenset({"knowledge_run"}),
    )
    return ToolExecutionContext(
        workspace_context=workspace_context,
        agent_version_id=uuid4(),
        tool_call_id="tool-call",
    )


def _definition(snapshot_id: UUID, retrieval_config: dict[str, object]) -> ToolDefinition:
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
        snapshot_refs=(
            {
                "snapshot_id": str(snapshot_id),
                "snapshot_hash": "a" * 64,
            },
        ),
        retrieval_config=retrieval_config,
    )


def _agent(*, retrieval_config: dict[str, object]) -> Agent:
    return Agent(
        id=uuid4(),
        workspace_id=uuid4(),
        name="Budget Agent",
        system_prompt="Answer from the published snapshot.",
        prompt_version=1,
        model_profile_id=uuid4(),
        knowledge_binding_mode="PINNED",
        model_retry_policy={"max_attempts": 1},
        retrieval_config=retrieval_config,
        runtime_config={},
    )


def _profile(agent: Agent) -> ResolvedModelProfile:
    return ResolvedModelProfile(
        id=agent.model_profile_id,
        workspace_id=agent.workspace_id,
        provider_credential_id=uuid4(),
        model="offline",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("5"),
        fallback_profile_id=None,
        capabilities=ModelCapabilities(tool_calling=True),
    )


def _stage(chunk_ids: tuple[str, ...]) -> RetrievalTraceStage:
    return RetrievalTraceStage(
        latency_ms=1.0,
        results=tuple(
            RetrievalTraceResult(chunk_id=chunk_id, rank=rank, score=1 / rank)
            for rank, chunk_id in enumerate(chunk_ids, start=1)
        ),
    )


def _trace(
    *,
    fusion: tuple[str, ...],
    rerank: tuple[str, ...],
    selection: tuple[str, ...] | None = None,
) -> RetrievalTrace:
    empty = _stage(())
    return RetrievalTrace(
        snapshot_id="snapshot",
        dense=empty,
        sparse=empty,
        fusion=_stage(fusion),
        rerank=_stage(rerank),
        total_latency_ms=4.0,
        selection=_stage(selection) if selection is not None else None,
    )


def _evidence(chunk_id: str) -> RetrievedEvidence:
    return RetrievedEvidence(
        document_id="document",
        document_revision_id="revision",
        chunk_id=chunk_id,
        source="source",
        locator={},
        text="evidence",
        retrieval_score=1.0,
        rerank_score=1.0,
        metadata={},
    )
