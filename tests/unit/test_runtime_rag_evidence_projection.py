from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.agent_runtime.rag_evidence import project_rag_evidence_payload
from packages.agent_runtime.runtime import _AgentRunGraph
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolRisk,
    ToolSourceKind,
)


@dataclass(frozen=True)
class _CharEstimator:
    def estimate(self, text: str) -> int:
        return len(text)


@dataclass(frozen=True)
class _Utf8ByteEstimator:
    def estimate(self, text: str) -> int:
        return len(text.encode("utf-8"))


def _identifiers(index: int) -> dict[str, str]:
    snapshot_id = str(UUID(int=index + 1))
    return {
        "snapshot_id": snapshot_id,
        "knowledge_base_id": str(UUID(int=index + 101)),
        "document_revision_id": str(UUID(int=index + 201)),
        "chunk_id": f"chunk-{index}",
    }


def _record(index: int, text: str = "evidence") -> dict[str, object]:
    identifiers = _identifiers(index)
    return {
        "chunk_id": identifiers["chunk_id"],
        "snapshot_id": identifiers["snapshot_id"],
        "document_id": f"document-{index}",
        "document_revision_id": identifiers["document_revision_id"],
        "snippet": text,
    }


def _payload(
    records: list[dict[str, object]],
    references: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "data": {"results": records, "evidence_refs": references},
    }


def _definition(
    *,
    identity: str = "search_knowledge",
    context_budget_bytes: object = 8_000,
    source_kind: ToolSourceKind = ToolSourceKind.BUILTIN,
) -> ToolDefinition:
    return ToolDefinition(
        identity=identity,
        revision_id=uuid4(),
        spec_hash="spec-hash",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        retrieval_config={"context_budget_bytes": context_budget_bytes},
        source_kind=source_kind,
    )


def _graph(
    *,
    runtime_context_budget: dict[str, int] | None = None,
    context_limit: int = 20_000,
) -> _AgentRunGraph:
    graph = object.__new__(_AgentRunGraph)
    graph.sequence = 0
    graph.service = SimpleNamespace()

    async def step(*_args, **_kwargs):
        return None

    graph.step = step
    graph._runtime_context_budget = runtime_context_budget or {
        "reserved_output_tokens": 1,
        "max_retrieval_tokens": 5_000,
        "max_tool_result_tokens": 4_000,
    }
    graph._context_limit = context_limit
    return graph


def _model_graph(*, request_sink: list[ModelRequest]) -> _AgentRunGraph:
    graph = _graph()

    async def stop_if_requested():
        return None

    async def touch(*_args, **_kwargs):
        return None

    async def emit(*_args, **_kwargs):
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _Gateway:
        async def generate_resolved(self, _context, _plan, request):
            request_sink.append(request)
            return ModelResponse(content="done", provider="test", model="test-model")

    graph._stop_if_requested = stop_if_requested
    graph._touch_admitted_memories = touch
    graph._emit = emit
    graph.model_round_count = 0
    graph.usage_records = []
    graph.mode = "sync"
    graph.context = SimpleNamespace(workspace_id=str(uuid4()))
    graph.run = SimpleNamespace(id=uuid4(), agent_version_id=uuid4())
    graph.service = SimpleNamespace(
        session_factory=lambda: _Session(),
        model_gateway_factory=lambda _session: _Gateway(),
        trace_sink=None,
    )
    graph._runtime_context_budget = {
        "reserved_output_tokens": 1,
        "max_retrieval_tokens": 5_000,
        "max_tool_result_tokens": 4_000,
    }
    graph._context_limit = 20_000
    return graph


def _observation_state(
    graph: _AgentRunGraph,
    *,
    definition: ToolDefinition,
    payload: dict[str, object],
) -> dict:
    call_id = "call-1"
    return {
        "messages": [
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(
                        name=definition.identity,
                        arguments={},
                        provider_tool_call_id=call_id,
                    ),
                ),
            )
        ],
        "proposed_tool_calls": [
            {"name": definition.identity, "tool_call_id": call_id, "arguments": {}}
        ],
        "executed_observations": {
            call_id: ToolResult.success(payload["data"]),
        },
        "runtime": {"context_budget": graph._runtime_context_budget},
        "tool_definitions": {definition.identity: definition},
    }


@pytest.mark.asyncio
async def test_observation_keeps_matched_refs_and_unicode_without_preview() -> None:
    graph = _graph()
    definition = _definition()
    references = [_identifiers(1), _identifiers(2)]
    records = [_record(1, "正文一"), _record(2, "正文二")]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, references),
    )
    update = await graph.observation(state)

    message = update["messages"][-1]
    payload = json.loads(message.content)
    assert payload["evidence_format"] == "rag_records_v1"
    assert payload["trust"] == "UNTRUSTED"
    assert payload["data"]["results"] == records
    assert payload["data"]["evidence_refs"] == references
    assert "正文一" in message.content
    assert "preview" not in payload["data"]


@pytest.mark.asyncio
async def test_observation_uses_category_budget_and_skips_oversized_record() -> None:
    graph = _graph(
        runtime_context_budget={
            "reserved_output_tokens": 1,
            "max_retrieval_tokens": 1_000,
            "max_tool_result_tokens": 80,
        }
    )
    definition = _definition()
    references = [_identifiers(1), _identifiers(2), _identifiers(3)]
    records = [
        _record(1, "x" * 4_000),
        _record(2, "small"),
        _record(3, "later"),
    ]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, references),
    )
    update = await graph.observation(state)

    payload = json.loads(update["messages"][-1].content)
    assert [record["chunk_id"] for record in payload["data"]["results"]] == [
        "chunk-2",
        "chunk-3",
    ]
    assert [reference["chunk_id"] for reference in payload["data"]["evidence_refs"]] == [
        "chunk-2",
        "chunk-3",
    ]
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_observation_without_explicit_budget_uses_projection() -> None:
    graph = _graph()
    definition = _definition(context_budget_bytes=None)
    references = [_identifiers(1), _identifiers(2)]
    records = [_record(1, "正文一"), _record(2, "正文二")]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, references),
    )
    update = await graph.observation(state)

    payload = json.loads(update["messages"][-1].content)
    assert payload["evidence_format"] == "rag_records_v1"
    assert payload["data"]["results"] == records
    assert payload["data"]["evidence_refs"] == references
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_observation_default_budget_measures_real_record_delivery() -> None:
    # Production-shaped system assertion: five 1200-char (2400-byte UTF-8)
    # chunks through the unconfigured default path must deliver whole paired
    # records under the 5000-byte evidence pool, never a JSON preview prefix.
    graph = _graph()
    definition = _definition(context_budget_bytes=None)
    references = [_identifiers(index) for index in range(1, 6)]
    records = [_record(index, "证" * 1_200) for index in range(1, 6)]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, references),
    )
    update = await graph.observation(state)

    payload = json.loads(update["messages"][-1].content)
    assert payload["evidence_format"] == "rag_records_v1"
    delivered = payload["data"]["results"]
    # Approx-token accounting fits every 1200-char Chinese record (each
    # ~750 units) into the 5000-unit default evidence pool.
    assert delivered == records
    assert payload["data"]["evidence_refs"] == references
    assert payload["truncated"] is False
    assert ApproxTokenEstimator().estimate(update["messages"][-1].content) <= 5_000


@pytest.mark.asyncio
async def test_observation_all_oversized_records_yield_empty_envelope_not_preview() -> None:
    graph = _graph()
    definition = _definition(context_budget_bytes=None)
    references = [_identifiers(1)]
    records = [_record(1, "x" * 24_000)]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, references),
    )
    update = await graph.observation(state)

    payload = json.loads(update["messages"][-1].content)
    assert payload == {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": True,
        "data": {"results": [], "evidence_refs": []},
    }


@pytest.mark.asyncio
async def test_observation_leaves_unknown_tool_and_error_results_on_legacy_path() -> None:
    graph = _graph()
    definition = _definition(identity="lookup")
    payload = _payload([_record(1)], [_identifiers(1)])

    state = _observation_state(
        graph,
        definition=definition,
        payload=payload,
    )
    update = await graph.observation(state)
    legacy = json.loads(update["messages"][-1].content)
    assert "evidence_format" not in legacy

    error_state = _observation_state(
        graph,
        definition=_definition(),
        payload=payload,
    )
    error_state["executed_observations"]["call-1"] = ToolResult.failure(
        "SEARCH_FAILED", "Search failed."
    )
    error_update = await graph.observation(error_state)
    error = json.loads(error_update["messages"][-1].content)
    assert error["status"] == "ERROR"
    assert "evidence_format" not in error


@pytest.mark.asyncio
async def test_observation_projection_failure_is_distinguishable_marker() -> None:
    graph = _graph()
    definition = _definition()
    records = [_record(1)]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, []),
    )
    update = await graph.observation(state)

    payload = json.loads(update["messages"][-1].content)
    assert payload == {
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": True,
        "evidence_projection": "FAILED",
    }
    assert "data" not in payload


@pytest.mark.asyncio
async def test_observation_non_builtin_source_uses_legacy_path() -> None:
    graph = _graph()
    definition = _definition(source_kind=ToolSourceKind.MCP)
    payload = _payload([_record(1)], [_identifiers(1)])

    state = _observation_state(
        graph,
        definition=definition,
        payload=payload,
    )
    update = await graph.observation(state)

    content = json.loads(update["messages"][-1].content)
    assert "evidence_format" not in content
    assert content["data"]["results"] == [_record(1)]


@pytest.mark.asyncio
async def test_observation_definition_identity_must_match_call_name() -> None:
    graph = _graph()
    definition = _definition(identity="lookup")
    payload = _payload([_record(1)], [_identifiers(1)])

    state = _observation_state(
        graph,
        definition=definition,
        payload=payload,
    )
    state["tool_definitions"] = {"search_knowledge": definition}
    state["proposed_tool_calls"][0]["name"] = "search_knowledge"

    update = await graph.observation(state)

    content = json.loads(update["messages"][-1].content)
    assert "evidence_format" not in content


@pytest.mark.asyncio
async def test_observation_invalid_budget_values_fall_back_to_pool_projection() -> None:
    payload = _payload([_record(1)], [_identifiers(1)])

    for configured_bytes in (True, 1, 64_001, 8_000.0, "8000"):
        graph = _graph()
        definition = _definition(context_budget_bytes=configured_bytes)
        state = _observation_state(
            graph,
            definition=definition,
            payload=payload,
        )
        update = await graph.observation(state)

        content = json.loads(update["messages"][-1].content)
        assert content["evidence_format"] == "rag_records_v1"
        assert content["data"]["results"] == [_record(1)]


@pytest.mark.asyncio
async def test_observation_clamped_budget_logs_two_layer_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    graph = _graph(
        runtime_context_budget={
            "reserved_output_tokens": 1,
            "max_retrieval_tokens": 1_500,
            "max_tool_result_tokens": 4_000,
        }
    )
    definition = _definition(context_budget_bytes=8_000)
    references = [_identifiers(1), _identifiers(2)]
    records = [_record(1, "a" * 4_000), _record(2, "b" * 4_000)]

    state = _observation_state(
        graph,
        definition=definition,
        payload=_payload(records, references),
    )
    with caplog.at_level("WARNING", logger="packages.agent_runtime.runtime"):
        update = await graph.observation(state)

    assert any(
        record.message == "search_evidence_budget_clamped_to_runtime_pool"
        for record in caplog.records
    )
    payload = json.loads(update["messages"][-1].content)
    # The clamp is real: only what fits the 1500-byte pool is delivered.
    assert len(payload["data"]["results"]) == 1


@pytest.mark.asyncio
async def test_model_admission_keeps_tool_definition_for_opt_in_observation() -> None:
    definition = _definition()
    requests: list[ModelRequest] = []
    graph = _model_graph(request_sink=requests)
    references = [_identifiers(1)]
    records = [_record(1, "完整正文")]
    state = {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            ModelMessage(role="user", content="question"),
        ],
        "spec": _frozen_spec(20_000),
        "runtime": {
            "max_steps": 5,
            "context_budget": graph._runtime_context_budget,
        },
        "tool_definitions": {"search_knowledge": definition},
        "memory_message_count": 0,
        "history_message_count": 0,
        "model_round_count": 0,
        "usage_records": [],
    }

    await graph.model(state)
    assert requests
    assert requests[0].tools[0].name == "search_knowledge"
    assert state["tool_definitions"]["search_knowledge"] is definition

    call_id = "call-1"
    observation_state = {
        "messages": [
            *state["messages"],
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(
                        name="search_knowledge",
                        arguments={},
                        provider_tool_call_id=call_id,
                    ),
                ),
            ),
        ],
        "proposed_tool_calls": [
            {"name": "search_knowledge", "tool_call_id": call_id, "arguments": {}}
        ],
        "executed_observations": {
            call_id: ToolResult.success(
                _payload(records, references)["data"]
            )
        },
        "runtime": state["runtime"],
        "tool_definitions": state["tool_definitions"],
    }
    update = await graph.observation(observation_state)
    payload = json.loads(update["messages"][-1].content)

    assert payload["evidence_format"] == "rag_records_v1"
    assert payload["data"] == {"results": records, "evidence_refs": references}


def test_helper_rejects_dangling_ambiguous_and_duplicate_identity() -> None:
    reference = _identifiers(1)
    record = _record(1)

    assert project_rag_evidence_payload(
        _payload([record], []), 4_000, _CharEstimator()
    ) is None
    assert project_rag_evidence_payload(
        _payload([record], [reference, reference]), 4_000, _CharEstimator()
    ) is None
    assert project_rag_evidence_payload(
        _payload([record, record], [reference]), 4_000, _CharEstimator()
    ) is None


def test_helper_all_records_oversized_returns_paired_empty_truncated_envelope() -> None:
    reference = _identifiers(1)
    record = _record(1, "x" * 10_000)

    projected = project_rag_evidence_payload(
        _payload([record], [reference]), 200, _CharEstimator()
    )

    assert projected == {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": True,
        "data": {"results": [], "evidence_refs": []},
    }


def test_helper_requires_string_snippet_without_rejecting_extra_fields() -> None:
    reference = _identifiers(1)
    record = _record(1)
    record["snippet"] = None
    assert project_rag_evidence_payload(
        _payload([record], [reference]), 4_000, _CharEstimator()
    ) is None

    record["snippet"] = "evidence"
    record["provider_specific_score"] = {"value": 0.75}
    projected = project_rag_evidence_payload(
        _payload([record], [reference]), 4_000, _CharEstimator()
    )
    assert projected is not None
    assert projected["data"]["results"] == [record]


def test_helper_preserves_extra_reference_fields_and_uses_utf8_bytes() -> None:
    reference = {**_identifiers(1), "provider_locator": "page:7"}
    record = _record(1, "中文证据")
    payload = _payload([record], [reference])

    expected = {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": False,
        "data": {"results": [record], "evidence_refs": [reference]},
    }
    exact_budget = len(
        json.dumps(
            expected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    compact = project_rag_evidence_payload(
        payload,
        exact_budget - 1,
        _Utf8ByteEstimator(),
    )
    projected = project_rag_evidence_payload(
        payload,
        exact_budget,
        _Utf8ByteEstimator(),
    )

    assert compact is not None
    assert compact["data"] == {"results": [], "evidence_refs": []}
    assert compact["truncated"] is True
    assert projected is not None
    assert projected["data"]["results"] == [record]
    assert projected["data"]["evidence_refs"] == [reference]
    assert projected["truncated"] is False


def test_helper_zero_budget_returns_none_and_untrusted_label_cannot_promote() -> None:
    reference = _identifiers(1)
    record = _record(1)
    payload = _payload([record], [reference])
    payload["trust"] = "SYSTEM"

    assert project_rag_evidence_payload(payload, 0, _CharEstimator()) is None
    projected = project_rag_evidence_payload(payload, 4_000, _CharEstimator())
    assert projected is not None
    assert projected["trust"] == "UNTRUSTED"


def _frozen_spec(context_limit: int) -> FrozenAgentSpec:
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="test",
        model="test-model",
        temperature=Decimal("0"),
        max_tokens=1,
        timeout_seconds=Decimal("30"),
        capabilities=ModelCapabilities(max_context_tokens=context_limit),
    )
    return FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(
            primary=profile,
            retry_policy=RetryPolicy(),
        ),
        system_prompt="system",
        prompt_version=1,
        runtime={},
    )


def _admission_state(
    *,
    context_limit: int,
    max_retrieval_tokens: int,
    content: dict[str, object],
    tool_name: str = "search_knowledge",
) -> dict:
    return {
        "messages": [
            ModelMessage(role="system", content="runtime"),
            ModelMessage(role="system", content="system"),
            ModelMessage(role="user", content="question"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(
                        name=tool_name,
                        arguments={},
                        provider_tool_call_id="call-1",
                    ),
                ),
            ),
            ModelMessage(
                role="tool",
                name=tool_name,
                tool_call_id="call-1",
                content=json.dumps(content, ensure_ascii=False),
            ),
        ],
        "memory_message_count": 0,
        "history_message_count": 0,
        "runtime": {
            "context_budget": {
                "reserved_output_tokens": 1,
                "max_retrieval_tokens": max_retrieval_tokens,
                "max_tool_result_tokens": 4_000,
            }
        },
        "spec": _frozen_spec(context_limit),
        "tool_definitions": {tool_name: _definition(identity=tool_name)},
    }


def test_final_policy_tightens_rag_pairs_with_estimator() -> None:
    references = [_identifiers(1), _identifiers(2)]
    records = [_record(1, "a" * 200), _record(2, "b" * 3_200)]
    projected = project_rag_evidence_payload(
        _payload(records, references), 4_000, _CharEstimator()
    )
    assert projected is not None

    state = _admission_state(
        context_limit=20_000,
        max_retrieval_tokens=800,
        content=projected,
    )
    graph = _graph(context_limit=20_000)
    admitted = graph._admit_context(state)
    result = json.loads(admitted.messages[-1].content)

    assert len(result["data"]["results"]) == 1
    assert result["data"]["results"][0]["chunk_id"] == "chunk-1"
    assert len(result["data"]["results"]) == len(result["data"]["evidence_refs"])
    assert [r["chunk_id"] for r in result["data"]["results"]] == [
        r["chunk_id"] for r in result["data"]["evidence_refs"]
    ]


def test_final_policy_preserves_existing_truncated_true() -> None:
    references = [_identifiers(1), _identifiers(2)]
    records = [_record(1, "a" * 200), _record(2, "b" * 3_200)]
    projected = project_rag_evidence_payload(
        _payload(records, references), 4_000, _Utf8ByteEstimator()
    )
    assert projected is not None
    projected["truncated"] = True

    state = _admission_state(
        context_limit=20_000,
        max_retrieval_tokens=800,
        content=projected,
    )
    admitted = _graph(context_limit=20_000)._admit_context(state)
    result = json.loads(admitted.messages[-1].content)

    assert result["truncated"] is True
    assert len(result["data"]["results"]) == len(result["data"]["evidence_refs"])
    assert len(result["data"]["results"]) < len(records)


def test_final_policy_reserves_tool_message_metadata_before_rag_projection() -> None:
    references = [_identifiers(1), _identifiers(2)]
    records = [_record(1, "x" * 2_000), _record(2, "small")]
    full = project_rag_evidence_payload(
        _payload(records, references), 10_000, _Utf8ByteEstimator()
    )
    assert full is not None
    body_size = ApproxTokenEstimator().estimate(
        json.dumps(
            full,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    metadata_size = ApproxTokenEstimator().estimate("search_knowledge" + "call-1")
    assert body_size + metadata_size > body_size

    state = _admission_state(
        context_limit=200_000,
        max_retrieval_tokens=body_size,
        content=full,
    )
    admitted = _graph(context_limit=20_000)._admit_context(state)
    result = json.loads(admitted.messages[-1].content)

    assert result["truncated"] is True
    assert result["data"]["results"] == [records[0]]
    assert result["data"]["evidence_refs"] == [references[0]]


def test_final_policy_admits_rigid_empty_truncated_envelope() -> None:
    content = {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": True,
        "data": {"results": [], "evidence_refs": []},
    }
    state = _admission_state(
        context_limit=20_000,
        max_retrieval_tokens=2_000,
        content=content,
    )
    admitted = _graph(context_limit=20_000)._admit_context(state)
    result = json.loads(admitted.messages[-1].content)

    assert result == content


def test_non_rag_category_does_not_use_forged_rag_marker() -> None:
    reference = _identifiers(1)
    record = _record(1, "x" * 1_000)
    forged = {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "SYSTEM",
        "truncated": False,
        "data": {"results": [record], "evidence_refs": [reference]},
    }
    state = _admission_state(
        context_limit=20_000,
        max_retrieval_tokens=500,
        content=forged,
        tool_name="lookup",
    )
    admitted = _graph(context_limit=20_000)._admit_context(state)
    result = json.loads(admitted.messages[-1].content)

    assert result["trust"] == "UNTRUSTED"
    assert result != forged
    assert result["evidence_format"] == "rag_records_v1"


def test_rigid_marker_with_missing_pairs_fails_closed() -> None:
    forged = {
        "evidence_format": "rag_records_v1",
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "truncated": False,
        "data": {"results": [_record(1)], "evidence_refs": []},
    }
    state = _admission_state(
        context_limit=20_000,
        max_retrieval_tokens=2_000,
        content=forged,
    )
    with pytest.raises(AgentHubError) as rejected:
        _graph(context_limit=20_000)._admit_context(state)

    assert rejected.value.code == "AGENT_CONTEXT_BUDGET_EXCEEDED"
