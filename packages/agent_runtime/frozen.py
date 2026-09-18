"""Parsing and validation for immutable AgentVersion runtime projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.capabilities import capabilities_from_mapping
from packages.model_gateway.contracts import (
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)

_DEFAULT_RUNTIME = {
    "max_steps": 8,
    "max_tool_calls": 12,
    "max_identical_calls": 2,
    "max_parallel_reads": 3,
}
_DEFAULT_CONTEXT_BUDGET = {
    "reserved_output_tokens": 2_000,
    "max_retrieval_tokens": 5_000,
    "max_tool_result_tokens": 4_000,
}


@dataclass(frozen=True, slots=True)
class FrozenAgentSpec:
    model_plan: ResolvedModelExecutionPlan
    system_prompt: str
    prompt_version: int
    runtime: dict[str, Any]


def parse_frozen_agent_spec(
    resolved_spec: Mapping[str, Any], *, workspace_id: UUID
) -> FrozenAgentSpec:
    if not isinstance(resolved_spec, Mapping):
        raise _invalid_binding()
    model = resolved_spec.get("model")
    prompt = resolved_spec.get("prompt")
    if not isinstance(model, Mapping) or not isinstance(prompt, Mapping):
        raise _invalid_binding()
    primary = _parse_profile(model, workspace_id=workspace_id, primary=True)
    fallback_values = model.get("fallback_profiles", [])
    if not isinstance(fallback_values, list):
        raise _invalid_binding()
    fallbacks = tuple(
        _parse_profile(item, workspace_id=workspace_id, primary=False) for item in fallback_values
    )
    retry_value = model.get("retry_policy", {"max_attempts": 1})
    if not isinstance(retry_value, Mapping):
        raise _invalid_binding()
    try:
        retry_policy = RetryPolicy(max_attempts=int(retry_value.get("max_attempts", 1)))
    except (TypeError, ValueError):
        raise _invalid_binding() from None
    system_prompt = prompt.get("system_prompt", prompt.get("system"))
    prompt_version = prompt.get("prompt_version", prompt.get("version", 1))
    if (
        not isinstance(system_prompt, str)
        or not system_prompt
        or not isinstance(prompt_version, int)
    ):
        raise _invalid_binding()
    runtime_value = resolved_spec.get("runtime", {})
    if not isinstance(runtime_value, Mapping):
        raise _invalid_binding()
    runtime: dict[str, Any] = {}
    for key, default in _DEFAULT_RUNTIME.items():
        value = runtime_value.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _invalid_binding()
        runtime[key] = value
    context_budget_value = runtime_value.get("context_budget", _DEFAULT_CONTEXT_BUDGET)
    if not isinstance(context_budget_value, Mapping):
        raise _invalid_binding()
    if not set(context_budget_value).issubset(_DEFAULT_CONTEXT_BUDGET):
        raise _invalid_binding()
    context_budget: dict[str, int] = {}
    for key, default in _DEFAULT_CONTEXT_BUDGET.items():
        value = context_budget_value.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise _invalid_binding()
        context_budget[key] = value
    runtime["context_budget"] = context_budget
    return FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(
            primary=primary,
            fallbacks=fallbacks,
            retry_policy=retry_policy,
        ),
        system_prompt=system_prompt,
        prompt_version=prompt_version,
        runtime=runtime,
    )


def _parse_profile(
    value: Any, *, workspace_id: UUID, primary: bool
) -> ResolvedModelExecutionProfile:
    if not isinstance(value, Mapping):
        raise _invalid_binding()
    required = {
        "profile_id",
        "credential_ref",
        "provider",
        "model",
        "temperature",
        "max_tokens",
        "timeout_seconds",
        "capabilities",
    }
    if not required.issubset(value):
        raise _invalid_binding()
    try:
        profile_id = UUID(str(value["profile_id"]))
        credential_ref = UUID(str(value["credential_ref"]))
        capabilities = capabilities_from_mapping(value["capabilities"])
        temperature = Decimal(str(value["temperature"]))
        timeout_seconds = Decimal(str(value["timeout_seconds"]))
        max_tokens = value["max_tokens"]
        provider = value["provider"]
        model = value["model"]
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
            or isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
            or temperature < 0
            or timeout_seconds <= 0
        ):
            raise ValueError
    except (ArithmeticError, KeyError, TypeError, ValueError):
        raise _invalid_binding() from None
    return ResolvedModelExecutionProfile(
        id=profile_id,
        workspace_id=workspace_id,
        provider_credential_id=credential_ref,
        provider=provider,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        capabilities=capabilities,
    )


def _invalid_binding() -> AgentHubError:
    return AgentHubError(
        "AGENT_VERSION_MODEL_BINDING_INVALID",
        "The published model binding is invalid.",
        422,
    )


__all__ = ["FrozenAgentSpec", "parse_frozen_agent_spec"]
