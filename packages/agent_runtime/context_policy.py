"""Pure M2 context policy normalization, budgeting, and freeze validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from packages.model_gateway.capabilities import capabilities_from_mapping
from packages.model_gateway.context_metadata import (
    ContextMetadata,
    InputOutputSemantics,
    context_metadata_to_mapping,
)

POLICY_VERSION = "adaptive_context_v1"
PRESET_INPUT_LIMITS: dict[str, int] = {
    "GENERAL_64K": 65_536,
    "STANDARD_32K": 32_768,
    "LONG_DOCUMENT_128K": 131_072,
}
DEFAULT_PRESET = "GENERAL_64K"
THRESHOLD_PERCENTS: dict[str, int] = {
    "soft_trigger_percent": 70,
    "hard_trigger_percent": 85,
    "target_percent": 55,
}
WORKSPACE_INPUT_LIMIT_SOURCES = frozenset({"UNSET", "DEFAULT", "CUSTOM"})
COUNTER_ID = "approx_language_v1"
COUNTER_VERSION = 1
_POLICY_KEYS = frozenset(
    {
        "policy_version",
        "preset",
        "workspace_input_limit",
        "workspace_input_limit_source",
        "soft_trigger_percent",
        "hard_trigger_percent",
        "target_percent",
        "counter_id",
    }
)


@dataclass(frozen=True, slots=True)
class _ResolvedModelContext:
    profile_id: str
    credential_ref: str
    provider: str
    model: str
    requested_max_tokens: int
    capabilities: Any
    metadata: ContextMetadata | None
    total_context_tokens: int | None
    max_input_tokens: int | None
    deployment_input_limit: int | None
    output_allowance: int
    applicable_input_limit: int
    safety_reserve_tokens: int
    available_input_tokens: int


def normalize_context_policy_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a user policy request without accepting budget proofs.

    The function is idempotent.  Draft-shaped UNSET/DEFAULT requests omit the
    numeric limit so persistence cannot turn default intent into CUSTOM.
    Already-resolved frozen values are accepted only when they agree with that
    source; CUSTOM preserves an explicit value even when it equals the preset.
    """

    if not isinstance(value, Mapping):
        raise ValueError("context policy must be a mapping")
    if set(value) - _POLICY_KEYS:
        raise ValueError("context policy contains unsupported fields")
    policy_version = value.get("policy_version", POLICY_VERSION)
    if policy_version != POLICY_VERSION:
        raise ValueError("policy_version must be adaptive_context_v1")
    preset = value.get("preset", DEFAULT_PRESET)
    if preset not in PRESET_INPUT_LIMITS:
        raise ValueError("preset is invalid")
    # An explicit null is the API's clear operation, not a custom value.
    # Treating key presence alone as intent would turn a cleared draft into
    # CUSTOM on the next normalization pass.
    workspace_limit_value = value.get("workspace_input_limit")
    workspace_limit_present = workspace_limit_value is not None
    supplied_source = value.get("workspace_input_limit_source")
    if supplied_source is None:
        workspace_input_limit_source = "CUSTOM" if workspace_limit_present else "UNSET"
    elif supplied_source in WORKSPACE_INPUT_LIMIT_SOURCES:
        workspace_input_limit_source = supplied_source
    else:
        raise ValueError("workspace_input_limit_source is invalid")
    preset_limit = PRESET_INPUT_LIMITS[preset]
    if workspace_input_limit_source == "CUSTOM" and workspace_limit_value is None:
        raise ValueError("CUSTOM workspace limit requires workspace_input_limit")
    if workspace_limit_value is None:
        workspace_limit: int | None = None
    else:
        workspace_limit = _positive_int(workspace_limit_value, "workspace_input_limit")
        if workspace_limit > preset_limit:
            raise ValueError("workspace_input_limit exceeds the selected preset")
        if workspace_input_limit_source in {"UNSET", "DEFAULT"} and workspace_limit != preset_limit:
            raise ValueError(
                f"{workspace_input_limit_source} workspace limit must equal the resolved preset"
            )
    for name, expected in THRESHOLD_PERCENTS.items():
        actual = value.get(name, expected)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            raise ValueError(f"{name} only accepts the fixed value {expected}")
    counter_id = value.get("counter_id", COUNTER_ID)
    if counter_id != COUNTER_ID:
        raise ValueError("counter_id must be approx_language_v1")
    result = {
        "policy_version": POLICY_VERSION,
        "preset": preset,
        "workspace_input_limit_source": workspace_input_limit_source,
        "soft_trigger_percent": THRESHOLD_PERCENTS["soft_trigger_percent"],
        "hard_trigger_percent": THRESHOLD_PERCENTS["hard_trigger_percent"],
        "target_percent": THRESHOLD_PERCENTS["target_percent"],
        "counter_id": COUNTER_ID,
    }
    if workspace_input_limit_source == "CUSTOM":
        if workspace_limit is None:
            raise ValueError("CUSTOM workspace limit requires workspace_input_limit")
        result["workspace_input_limit"] = workspace_limit
    return result


def resolve_context_policy(
    request: Mapping[str, Any],
    model_chain: Sequence[Mapping[str, Any]],
    *,
    reserved_output_tokens: int,
) -> dict[str, Any]:
    """Resolve the frozen chain into explainable, intersected input budgets."""

    normalized_request = normalize_context_policy_request(request)
    reserved_output = _positive_int(reserved_output_tokens, "reserved_output_tokens")
    models = tuple(_resolve_model(item, reserved_output) for item in _chain_items(model_chain))
    common_available_input = min(model.available_input_tokens for model in models)
    workspace_limit: int = normalized_request.get(
        "workspace_input_limit",
        PRESET_INPUT_LIMITS[normalized_request["preset"]],
    )
    working_input_limit = min(common_available_input, workspace_limit)
    total_windows = [
        model.total_context_tokens for model in models if model.total_context_tokens is not None
    ]
    min_total = min(total_windows) if len(total_windows) == len(models) else None
    thresholds = {
        "soft": _scaled_threshold(working_input_limit, THRESHOLD_PERCENTS["soft_trigger_percent"]),
        "hard": _scaled_threshold(working_input_limit, THRESHOLD_PERCENTS["hard_trigger_percent"]),
        "target": _scaled_threshold(working_input_limit, THRESHOLD_PERCENTS["target_percent"]),
    }
    model_limitations = _model_limitations(models, workspace_limit)
    return {
        "policy_version": POLICY_VERSION,
        "preset": normalized_request["preset"],
        "workspace_input_limit": workspace_limit,
        "workspace_input_limit_source": normalized_request["workspace_input_limit_source"],
        "input_limit_tokens": working_input_limit,
        "model_available_input_tokens": common_available_input,
        "min_total_context_tokens": min_total,
        "reserved_output_tokens": max(model.output_allowance for model in models),
        "models": [_model_projection(model) for model in models],
        "thresholds": thresholds,
        "counter": _counter_projection(),
        "limitations": model_limitations,
        "unknowns": _unknowns(models, min_total),
    }


def validate_frozen_context_policy(
    value: Mapping[str, Any],
    model_chain: Sequence[Mapping[str, Any]],
    *,
    reserved_output_tokens: int,
) -> dict[str, Any]:
    """Validate a persisted policy against frozen chain data only.

    No catalog path, environment variable, or caller metadata source is read.
    The persisted per-model source records must exactly match the frozen chain.
    """

    if not isinstance(value, Mapping):
        raise ValueError("frozen context policy must be a mapping")
    required = {
        "policy_version",
        "preset",
        "workspace_input_limit",
        "workspace_input_limit_source",
        "input_limit_tokens",
        "model_available_input_tokens",
        "min_total_context_tokens",
        "reserved_output_tokens",
        "models",
        "thresholds",
        "counter",
        "limitations",
        "unknowns",
    }
    if set(value) != required:
        raise ValueError("frozen context policy is incomplete")
    request = normalize_context_policy_request(
        {
            "policy_version": value["policy_version"],
            "preset": value["preset"],
            "workspace_input_limit": value["workspace_input_limit"],
            "workspace_input_limit_source": value["workspace_input_limit_source"],
            "soft_trigger_percent": THRESHOLD_PERCENTS["soft_trigger_percent"],
            "hard_trigger_percent": THRESHOLD_PERCENTS["hard_trigger_percent"],
            "target_percent": THRESHOLD_PERCENTS["target_percent"],
            "counter_id": COUNTER_ID,
        }
    )
    source = value["workspace_input_limit_source"]
    preset_limit = PRESET_INPUT_LIMITS[request["preset"]]
    if source not in WORKSPACE_INPUT_LIMIT_SOURCES:
        raise ValueError("workspace_input_limit_source is invalid")
    frozen_workspace_limit = value["workspace_input_limit"]
    if source in {"UNSET", "DEFAULT"} and frozen_workspace_limit != preset_limit:
        raise ValueError(f"{source} workspace limit does not match its preset")
    if source == "CUSTOM" and frozen_workspace_limit > preset_limit:
        raise ValueError("CUSTOM workspace limit exceeds its preset")
    expected = resolve_context_policy(
        request, model_chain, reserved_output_tokens=reserved_output_tokens
    )
    if _canonical_json(value) != _canonical_json(expected):
        raise ValueError("frozen context policy no longer matches its source chain")
    return expected


def _resolve_model(
    raw: Mapping[str, Any],
    reserved_output_tokens: int,
) -> _ResolvedModelContext:
    required = {
        "profile_id",
        "credential_ref",
        "provider",
        "model",
        "max_tokens",
        "capabilities",
    }
    # Frozen parser passes the complete primary profile projection, while the
    # runtime admission boundary reconstructs a smaller mapping. Both shapes
    # carry the same six budget-defining fields; unrelated publication fields
    # are intentionally ignored here rather than silently reinterpreted.
    if not isinstance(raw, Mapping) or not required.issubset(raw):
        raise ValueError("model chain entries have an invalid schema")
    profile_id = _identity_text(raw["profile_id"], "profile_id")
    credential_ref = _identity_text(raw["credential_ref"], "credential_ref")
    provider = _identity_text(raw["provider"], "provider")
    model = _identity_text(raw["model"], "model")
    actual_max_tokens = _positive_int(raw["max_tokens"], "max_tokens")
    capabilities_value = raw["capabilities"]
    if not isinstance(capabilities_value, Mapping):
        raise ValueError("capabilities must be a mapping")
    capabilities = capabilities_from_mapping(capabilities_value)
    metadata = capabilities.context_metadata
    total_context_tokens = capabilities.max_context_tokens
    max_input_tokens = metadata.max_input_tokens if metadata is not None else None
    deployment_input_limit = metadata.deployment_input_limit if metadata is not None else None
    output_allowance = max(actual_max_tokens, reserved_output_tokens)
    if (
        metadata is not None
        and metadata.input_output_semantics is InputOutputSemantics.TOTAL_WINDOW
        and total_context_tokens is None
    ):
        raise ValueError("TOTAL_WINDOW requires an explicit max_context_tokens")
    if total_context_tokens is None and (
        metadata is None
        or metadata.input_output_semantics is not InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT
        or metadata.max_input_tokens is None
    ):
        raise ValueError(
            "input-only capacity requires explicit INDEPENDENT_INPUT_OUTPUT metadata "
            "with max_input_tokens"
        )
    if (
        metadata is not None
        and metadata.max_output_tokens is not None
        and output_allowance > metadata.max_output_tokens
    ):
        raise ValueError("output reserve exceeds the declared max_output_tokens")
    if (
        metadata is not None
        and metadata.output_reserve_semantics is None
        and metadata.max_output_tokens is not None
    ):
        raise ValueError("output reserve semantics are required")
    constraints: list[tuple[str, int]] = []
    if max_input_tokens is not None:
        constraints.append(("provider_max_input_tokens", max_input_tokens))
    if deployment_input_limit is not None:
        constraints.append(("deployment_input_limit", deployment_input_limit))
    if total_context_tokens is not None:
        constraints.append(("total_context_less_output", total_context_tokens - output_allowance))
    if not constraints:
        raise ValueError("model has no provable input capacity")
    applicable = min(value for _, value in constraints)
    if applicable <= 0:
        raise ValueError("model context budget is not positive")
    safety_reserve = max(1_024, (5 * applicable + 99) // 100)
    available = applicable - safety_reserve
    if available <= 0:
        raise ValueError("model context budget leaves no usable input after safety reserve")
    return _ResolvedModelContext(
        profile_id=profile_id,
        credential_ref=credential_ref,
        provider=provider,
        model=model,
        requested_max_tokens=actual_max_tokens,
        capabilities=capabilities,
        metadata=metadata,
        total_context_tokens=total_context_tokens,
        max_input_tokens=max_input_tokens,
        deployment_input_limit=deployment_input_limit,
        output_allowance=output_allowance,
        applicable_input_limit=applicable,
        safety_reserve_tokens=safety_reserve,
        available_input_tokens=available,
    )


def _model_projection(model: _ResolvedModelContext) -> dict[str, Any]:
    metadata = model.metadata
    source = {
        "declared": contract_metadata_dict(model.capabilities.context_metadata),
        "declared_max_context_tokens": model.total_context_tokens,
        "resolved_model": metadata.resolved_model if metadata is not None else "UNKNOWN",
        "source_version": metadata.source_version if metadata is not None else None,
        "catalog_ref": metadata.catalog_ref if metadata is not None else None,
        "source_records": (
            [dict(record) for record in metadata.source_records]
            if metadata is not None and metadata.source_records is not None
            else None
        ),
    }
    return {
        "profile_id": model.profile_id,
        "credential_ref": model.credential_ref,
        "provider": model.provider,
        "model": model.model,
        "resolved_model": source["resolved_model"],
        "source": source,
        "limits": {
            "requested_max_tokens": model.requested_max_tokens,
            "total_context_tokens": model.total_context_tokens,
            "max_input_tokens": model.max_input_tokens,
            "deployment_input_limit": model.deployment_input_limit,
            "max_output_tokens": metadata.max_output_tokens if metadata is not None else None,
            "input_output_semantics": (
                metadata.input_output_semantics.value
                if metadata is not None and metadata.input_output_semantics is not None
                else None
            ),
            "output_reserve_semantics": (
                metadata.output_reserve_semantics.value
                if metadata is not None and metadata.output_reserve_semantics is not None
                else None
            ),
        },
        "budget": {
            "reserved_output_tokens": model.output_allowance,
            "applicable_input_limit_tokens": model.applicable_input_limit,
            "safety_reserve_tokens": model.safety_reserve_tokens,
            "available_input_tokens": model.available_input_tokens,
        },
        "reasons": _model_reasons(model),
    }


def contract_metadata_dict(value: Any) -> dict[str, Any] | None:
    """Expose the frozen capability metadata as detached canonical JSON."""

    return context_metadata_to_mapping(value)


def _model_reasons(model: _ResolvedModelContext) -> list[str]:
    reasons: list[str] = []
    if model.total_context_tokens is None:
        reasons.append("TOTAL_CONTEXT_UNKNOWN")
    if (
        model.total_context_tokens is not None
        and model.applicable_input_limit == model.total_context_tokens - model.output_allowance
    ):
        reasons.append("TOTAL_WINDOW_MINUS_OUTPUT")
    if (
        model.max_input_tokens is not None
        and model.applicable_input_limit == model.max_input_tokens
    ):
        reasons.append("PROVIDER_INPUT_LIMIT")
    if (
        model.deployment_input_limit is not None
        and model.applicable_input_limit == model.deployment_input_limit
    ):
        reasons.append("DEPLOYMENT_INPUT_LIMIT")
    reasons.append("FIVE_PERCENT_SAFETY_RESERVE")
    return reasons


def _model_limitations(
    models: Sequence[_ResolvedModelContext],
    workspace_limit: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for model in models:
        if model.available_input_tokens < workspace_limit:
            result.append(
                {
                    "scope": "MODEL",
                    "profile_id": model.profile_id,
                    "code": "MODEL_INPUT_CAP_BELOW_WORKSPACE_LIMIT",
                    "workspace_input_limit": workspace_limit,
                    "model_available_input_tokens": model.available_input_tokens,
                }
            )
    return result


def _unknowns(
    models: Sequence[_ResolvedModelContext],
    min_total: int | None,
) -> list[str]:
    unknown: list[str] = []
    if min_total is None:
        unknown.append("MIN_TOTAL_CONTEXT_TOKENS")
    if any(model.metadata is None for model in models):
        unknown.append("MODEL_CONTEXT_METADATA")
    if any(
        model.metadata is not None and model.metadata.resolved_model == "UNKNOWN"
        for model in models
    ):
        unknown.append("RESOLVED_MODEL")
    return unknown


def _counter_projection() -> dict[str, Any]:
    return {
        "id": COUNTER_ID,
        "version": COUNTER_VERSION,
        "precision": "APPROXIMATE",
        "request_overhead_tokens": 8,
        "message_overhead_tokens": 4,
        "tool_overhead_tokens": 8,
        "includes_protocol_overhead": False,
        "scope": (
            "Approximate heuristic for system/user/assistant/tool text plus JSON schema; "
            "does not guarantee an exact provider token count. The fixed overheads are "
            "applied once by the runtime wrapper and are not included in this heuristic."
        ),
    }


def _chain_items(model_chain: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    if isinstance(model_chain, (str, bytes)) or not isinstance(model_chain, Sequence):
        raise ValueError("model_chain must be a non-empty sequence of mappings")
    items = tuple(model_chain)
    if not items:
        raise ValueError("model_chain must contain at least one executable model")
    if any(not isinstance(item, Mapping) for item in items):
        raise ValueError("model_chain entries must be mappings")
    return items


def _scaled_threshold(value: int, percent: int) -> int:
    if percent == 55:
        return (percent * value) // 100
    return (percent * value + 99) // 100


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _identity_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _canonical_json(value: Any) -> str:
    import json

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "COUNTER_ID",
    "DEFAULT_PRESET",
    "POLICY_VERSION",
    "PRESET_INPUT_LIMITS",
    "THRESHOLD_PERCENTS",
    "WORKSPACE_INPUT_LIMIT_SOURCES",
    "contract_metadata_dict",
    "normalize_context_policy_request",
    "resolve_context_policy",
    "validate_frozen_context_policy",
]
