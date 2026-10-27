from __future__ import annotations

import copy
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from apps.api.schemas.agents import MemoryConfigRequest
from packages.agent_runtime.context_policy import (
    normalize_context_policy_request,
    resolve_context_policy,
    validate_frozen_context_policy,
)
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.capabilities import (
    effective_capability_requirements,
    validate_capabilities,
)
from packages.model_gateway.context_metadata import (
    ContextMetadata,
    InputOutputSemantics,
    OutputReserveSemantics,
)
from packages.model_gateway.contracts import (
    CapabilityRequirements,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
    StructuredOutputSchema,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode

K = 1024
PROFILE = UUID("00000000-0000-0000-0000-000000000101")
CREDENTIAL = UUID("00000000-0000-0000-0000-000000000102")


def _metadata(
    *,
    deployment_id: str = "deployment-a",
    input_output_semantics: InputOutputSemantics = InputOutputSemantics.TOTAL_WINDOW,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = 16_384,
    deployment_input_limit: int | None = None,
    resolved_model: str = "exact-model",
    source_version: str = "catalog-2026-10-27",
) -> ContextMetadata:
    return ContextMetadata(
        deployment_id=deployment_id,
        resolved_model=resolved_model,
        source_version=source_version,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        deployment_input_limit=deployment_input_limit,
        input_output_semantics=input_output_semantics,
        output_reserve_semantics=OutputReserveSemantics.INCLUDES_REASONING,
        catalog_ref="tests/local-context-catalog-v1",
    )


def _model(
    *,
    profile_id: UUID = PROFILE,
    model: str = "exact-model",
    max_tokens: int = 4_096,
    total: int | None = 1_048_576,
    metadata: ContextMetadata | None = None,
) -> dict:
    capabilities: dict[str, object] = {
        "tool_calling": True,
        "streaming": True,
        "structured_output": True,
        "vision": True,
        "max_context_tokens": total,
    }
    metadata = metadata or _metadata()
    capabilities["context_metadata"] = metadata.as_dict()
    return {
        "profile_id": str(profile_id),
        "credential_ref": str(CREDENTIAL),
        "provider": "offline",
        "model": model,
        "max_tokens": max_tokens,
        "capabilities": capabilities,
    }


def _resolve(
    chain: list[dict],
    *,
    preset: str = "GENERAL_64K",
    workspace_input_limit: int | None = None,
    reserved_output_tokens: int = 4_096,
) -> dict:
    request = {"policy_version": "adaptive_context_v1", "preset": preset}
    if workspace_input_limit is not None:
        request["workspace_input_limit"] = workspace_input_limit
    return resolve_context_policy(
        request,
        chain,
        reserved_output_tokens=reserved_output_tokens,
    )


def _frozen_spec(schema_version: int, runtime: dict) -> dict:
    model = _model(total=1_048_576)
    model.update(
        {
            "temperature": 0.0,
            "timeout_seconds": 30.0,
            "retry_policy": {"max_attempts": 1},
            "fallback_chain": [],
            "fallback_profiles": [],
        }
    )
    return {
        "spec_schema_version": schema_version,
        "model": model,
        "prompt": {"system_prompt": "Answer from approved context.", "prompt_version": 1},
        "retrieval": {
            "knowledge_binding_mode": "PINNED",
            "knowledge_bindings": [],
            "knowledge_snapshot_ids": [],
            "knowledge_snapshots": [],
        },
        "tools": [],
        "runtime": runtime,
    }


@pytest.mark.parametrize("preset", ["GENERAL_64K", "STANDARD_32K", "LONG_DOCUMENT_128K"])
def test_policy_normalization_keeps_the_three_fixed_presets_without_a_draft_limit(
    preset: str,
) -> None:
    normalized = normalize_context_policy_request(
        {
            "policy_version": "adaptive_context_v1",
            "preset": preset,
            "soft_trigger_percent": 70,
            "hard_trigger_percent": 85,
            "target_percent": 55,
            "counter_id": "approx_language_v1",
        }
    )

    assert normalized["preset"] == preset
    assert "workspace_input_limit" not in normalized
    assert normalized["workspace_input_limit_source"] == "UNSET"
    assert normalized["soft_trigger_percent"] == 70
    assert normalized["hard_trigger_percent"] == 85
    assert normalized["target_percent"] == 55
    assert normalized["counter_id"] == "approx_language_v1"


@pytest.mark.parametrize(
    "policy_request",
    [
        {"policy_version": "adaptive_context_v2"},
        {"policy_version": "adaptive_context_v1", "preset": "GENERAL_128K"},
        {
            "policy_version": "adaptive_context_v1",
            "workspace_input_limit": 64 * K + 1,
        },
        {"policy_version": "adaptive_context_v1", "workspace_input_limit": True},
        {"policy_version": "adaptive_context_v1", "max_memory_tokens": 4_096},
    ],
)
def test_policy_request_rejects_version_drift_unknown_fields_and_unproven_limits(
    policy_request: dict,
) -> None:
    with pytest.raises(ValueError):
        normalize_context_policy_request(policy_request)


def test_large_capability_cannot_expand_the_64k_workspace() -> None:
    resolved = _resolve([_model()], preset="GENERAL_64K")

    assert resolved["input_limit_tokens"] == 64 * K
    assert resolved["model_available_input_tokens"] == 992_256
    assert resolved["workspace_input_limit"] == 64 * K
    assert resolved["reserved_output_tokens"] == 4_096


def test_32k_model_reduces_64k_preset_and_exposes_the_source() -> None:
    metadata = _metadata(max_output_tokens=4_096)
    resolved = _resolve([_model(total=32 * K, metadata=metadata)])

    assert resolved["input_limit_tokens"] == 32 * K - 4_096 - 1_434
    assert resolved["input_limit_tokens"] < 64 * K
    assert resolved["models"][0]["reasons"] == [
        "TOTAL_WINDOW_MINUS_OUTPUT",
        "FIVE_PERCENT_SAFETY_RESERVE",
    ]
    assert resolved["limitations"][0]["code"] == "MODEL_INPUT_CAP_BELOW_WORKSPACE_LIMIT"


def test_fallback_intersects_every_executable_model() -> None:
    fallback_id = uuid4()
    fallback = _model(
        profile_id=fallback_id,
        model="fallback-model",
        total=32 * K,
        metadata=_metadata(max_output_tokens=4_096),
    )
    resolved = _resolve([_model(), fallback])

    expected_fallback = 32 * K - 4_096 - 1_434
    assert resolved["model_available_input_tokens"] == expected_fallback
    assert resolved["input_limit_tokens"] == expected_fallback
    assert [model["profile_id"] for model in resolved["models"]] == [
        str(PROFILE),
        str(fallback_id),
    ]
    assert resolved["limitations"][0]["profile_id"] == str(fallback_id)


def test_catalog_and_deployment_limits_intersect_without_name_inference() -> None:
    metadata = _metadata(
        deployment_input_limit=48 * K,
        max_input_tokens=200 * K,
        max_output_tokens=4_096,
    )
    resolved = _resolve([_model(total=1_048_576, metadata=metadata)])

    assert resolved["input_limit_tokens"] == 48 * K - 2_458
    assert resolved["models"][0]["limits"]["deployment_input_limit"] == 48 * K
    assert "DEPLOYMENT_INPUT_LIMIT" in resolved["models"][0]["reasons"]
    assert resolved["models"][0]["resolved_model"] == "exact-model"


def test_independent_input_is_usable_but_output_only_cannot_prove_input() -> None:
    independent = _model(
        total=None,
        metadata=_metadata(
            input_output_semantics=InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT,
            max_input_tokens=32 * K,
            max_output_tokens=4_096,
        ),
    )
    resolved = _resolve([independent])
    assert resolved["input_limit_tokens"] == 32 * K - 1_639
    assert resolved["min_total_context_tokens"] is None
    assert "MIN_TOTAL_CONTEXT_TOKENS" in resolved["unknowns"]

    with pytest.raises(ValueError):
        _metadata(
            input_output_semantics=InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT,
            max_input_tokens=None,
            max_output_tokens=4_096,
        )


@pytest.mark.parametrize(
    ("reserved", "max_tokens", "max_output"),
    [
        (4_096, 4_096, 4_095),
        (0, 4_096, 16_384),
        (True, 4_096, 16_384),
    ],
)
def test_invalid_or_oversized_output_reserve_is_rejected(
    reserved: int, max_tokens: int, max_output: int
) -> None:
    with pytest.raises(ValueError):
        resolve_context_policy(
            {"policy_version": "adaptive_context_v1"},
            [_model(max_tokens=max_tokens, metadata=_metadata(max_output_tokens=max_output))],
            reserved_output_tokens=reserved,
        )


def test_thresholds_are_integer_ceil_for_soft_hard_and_floor_for_target() -> None:
    metadata = _metadata(max_output_tokens=4_096)
    exact = _resolve([_model(total=36 * K, metadata=metadata)])
    below = _resolve([_model(total=36 * K - 1, metadata=metadata)])
    above = _resolve([_model(total=36 * K + 1, metadata=metadata)])

    work = exact["input_limit_tokens"]
    assert work == 31_129
    assert exact["thresholds"] == {
        "soft": 21_791,
        "hard": 26_460,
        "target": 17_120,
    }
    assert below["thresholds"]["hard"] == 26_459
    assert above["thresholds"]["hard"] == 26_461


def test_frozen_policy_replays_its_source_chain_and_rejects_drift() -> None:
    chain = [_model()]
    frozen = copy.deepcopy(_resolve(chain))

    replay = validate_frozen_context_policy(
        frozen,
        chain,
        reserved_output_tokens=4_096,
    )
    assert replay["input_limit_tokens"] == 64 * K
    assert replay["models"][0]["source"] == frozen["models"][0]["source"]

    source_drift = copy.deepcopy(chain)
    source_drift[0]["capabilities"]["context_metadata"]["catalog_ref"] = "new-catalog-v2"
    with pytest.raises(ValueError):
        validate_frozen_context_policy(
            frozen,
            source_drift,
            reserved_output_tokens=4_096,
        )

    tampered_policy = copy.deepcopy(frozen)
    tampered_policy["thresholds"]["hard"] += 1
    with pytest.raises(ValueError):
        validate_frozen_context_policy(
            tampered_policy,
            chain,
            reserved_output_tokens=4_096,
        )


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        ({"long_term_memory": True}, "UNSET"),
        ({"long_term_memory": True, "max_memory_tokens_source": "DEFAULT"}, "DEFAULT"),
        ({"long_term_memory": True, "max_memory_tokens_source": "UNSET"}, "UNSET"),
        (
            {"long_term_memory": True, "max_memory_tokens": 1_500},
            "CUSTOM",
        ),
        (
            {
                "long_term_memory": True,
                "max_memory_tokens": 4_096,
                "max_memory_tokens_source": "CUSTOM",
            },
            "CUSTOM",
        ),
    ],
)
def test_memory_request_recorded_source_distinguishes_equal_legacy_numbers(
    payload: dict, accepted: str
) -> None:
    request = MemoryConfigRequest.model_validate(payload)

    assert request.max_memory_tokens_source == accepted
    assert request.max_memory_tokens == payload.get("max_memory_tokens")


@pytest.mark.parametrize(
    "payload",
    [
        {"max_memory_tokens": 0},
        {"max_memory_tokens": -1},
        {"max_memory_tokens": True},
        {"max_memory_tokens": 1_500, "max_memory_tokens_source": "UNSET"},
        {"max_memory_tokens": 1_500, "max_memory_tokens_source": "DEFAULT"},
        {"max_memory_tokens_source": "CUSTOM"},
        {"max_memory_tokens": 1_500, "max_memory_tokens_source": "GUESSED"},
    ],
)
def test_memory_request_rejects_value_source_contradictions(payload: dict) -> None:
    with pytest.raises(ValueError):
        MemoryConfigRequest.model_validate(payload)


def test_schema_three_and_schema_two_frozen_memory_have_distinct_contracts() -> None:
    new_policy = _resolve([_model()])
    new_policy_memory = _frozen_spec(
        3,
        {
            "context_budget": {
                "reserved_output_tokens": 4_096,
                "max_retrieval_tokens": 1_000,
                "max_tool_result_tokens": 1_000,
            },
            "context_policy": new_policy,
            "context_budget_sources": {
                "reserved_output_tokens": "CUSTOM",
                "max_retrieval_tokens": "CUSTOM",
                "max_tool_result_tokens": "CUSTOM",
            },
            "memory": {
                "thread_history_search": False,
                "long_term_memory": True,
                "max_memory_tokens": 4_096,
                "max_memory_tokens_source": "DEFAULT",
            },
        },
    )
    parsed = parse_frozen_agent_spec(new_policy_memory, workspace_id=uuid4())
    assert parsed.runtime["memory"]["max_memory_tokens"] == 4_096
    assert parsed.runtime["memory"]["max_memory_tokens_source"] == "DEFAULT"
    assert parsed.runtime["context_policy"]["input_limit_tokens"] == 64 * K

    legacy_memory = _frozen_spec(
        2,
        {
            "context_budget": {
                "reserved_output_tokens": 2_000,
                "max_retrieval_tokens": 5_000,
                "max_tool_result_tokens": 4_000,
            },
            "memory": {
                "thread_history_search": False,
                "long_term_memory": True,
                "max_memory_tokens": 8_388,
            },
        },
    )
    legacy = parse_frozen_agent_spec(legacy_memory, workspace_id=uuid4())
    assert legacy.runtime["memory"]["max_memory_tokens"] == 8_388
    assert "max_memory_tokens_source" not in legacy.runtime["memory"]
    assert "context_policy" not in legacy.runtime

    memory_off = _frozen_spec(2, {})
    setattr_memory = memory_off["runtime"]
    parse_frozen_agent_spec(memory_off, workspace_id=uuid4())
    assert "memory" not in setattr_memory


def test_unknown_schema_and_invalid_frozen_policy_fail_closed() -> None:
    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(_frozen_spec(4, {}), workspace_id=uuid4())

    spec = _frozen_spec(
        3,
        {
            "context_policy": {
                "policy_version": "adaptive_context_v1",
                "input_limit_tokens": True,
            }
        },
    )
    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(spec, workspace_id=uuid4())


def test_tool_schema_stream_and_vision_gates_survive_policy_changes() -> None:
    capabilities = ModelCapabilities(
        tool_calling=True,
        streaming=True,
        structured_output=True,
        vision=True,
        max_context_tokens=64 * K,
        context_metadata=_metadata(max_output_tokens=8_192),
    )
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="inspect"),),
        tools=(
            ModelToolDefinition(
                name="lookup",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ),
        response_schema=StructuredOutputSchema(
            name="inspection",
            json_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        ),
        required_capabilities=CapabilityRequirements(required=frozenset({"vision"})),
    )
    required = effective_capability_requirements(request, "stream")
    assert required.required == {"tool_calling", "structured_output", "streaming", "vision"}
    assert validate_capabilities(capabilities, required) == capabilities

    without_vision = replace(capabilities, vision=False)
    with pytest.raises(ModelGatewayError) as mismatch:
        validate_capabilities(without_vision, required)
    assert mismatch.value.code == ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH
