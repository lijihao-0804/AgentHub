"""Parsing and validation for immutable AgentVersion runtime projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MEMORY_CONFIG,
    DEFAULT_RUNTIME_LIMITS,
    MAX_RUN_COST_LIMIT_MICRO_USD,
    MAX_RUNTIME_LIMITS,
)
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.capabilities import capabilities_from_mapping
from packages.model_gateway.contracts import (
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)

_DEFAULT_RUNTIME = DEFAULT_RUNTIME_LIMITS
_DEFAULT_CONTEXT_BUDGET = DEFAULT_CONTEXT_BUDGET
_DEFAULT_MEMORY = DEFAULT_MEMORY_CONFIG
SUPPORTED_SPEC_SCHEMA_VERSIONS = frozenset({1, 2})


@dataclass(frozen=True, slots=True)
class FrozenKnowledgeBinding:
    knowledge_base_id: UUID | None
    binding_mode: str
    snapshot_id: UUID | None = None
    snapshot_hash: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenAgentSpec:
    model_plan: ResolvedModelExecutionPlan
    system_prompt: str
    prompt_version: int
    runtime: dict[str, Any]
    knowledge_bindings: tuple[FrozenKnowledgeBinding, ...] = ()


def parse_frozen_agent_spec(
    resolved_spec: Mapping[str, Any], *, workspace_id: UUID
) -> FrozenAgentSpec:
    if not isinstance(resolved_spec, Mapping):
        raise _invalid_binding()
    schema_version = resolved_spec.get("spec_schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in SUPPORTED_SPEC_SCHEMA_VERSIONS
    ):
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
    retrieval = resolved_spec.get("retrieval", {})
    if not isinstance(retrieval, Mapping):
        raise _invalid_binding()
    knowledge_bindings = _parse_knowledge_bindings(retrieval, schema_version)
    runtime_value = resolved_spec.get("runtime", {})
    if not isinstance(runtime_value, Mapping):
        raise _invalid_binding()
    runtime: dict[str, Any] = {}
    for key, default in _DEFAULT_RUNTIME.items():
        value = runtime_value.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_RUNTIME_LIMITS[key]
        ):
            raise _invalid_binding()
        runtime[key] = value
    # The cost ceiling is carried across the freeze boundary by hand rather than
    # with the scalar limits above: it is the one runtime value that may legally
    # be absent, and a published ceiling the executing spec does not carry is a
    # ceiling that does nothing.
    cost_limit = runtime_value.get("max_cost_micro_usd")
    if cost_limit is not None and (
        isinstance(cost_limit, bool)
        or not isinstance(cost_limit, int)
        or not 1 <= cost_limit <= MAX_RUN_COST_LIMIT_MICRO_USD
    ):
        raise _invalid_binding()
    runtime["max_cost_micro_usd"] = cost_limit
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
    # Absent means every flag is false, which is exactly what every version
    # published before memory existed meant. Unknown keys are rejected rather
    # than ignored: a flag this build does not understand is a flag it cannot
    # honour, and silently running without it would be the wrong answer to give
    # a spec that asked for it.
    memory_value = runtime_value.get("memory", _DEFAULT_MEMORY)
    if not isinstance(memory_value, Mapping):
        raise _invalid_binding()
    if not set(memory_value).issubset(_DEFAULT_MEMORY):
        raise _invalid_binding()
    memory: dict[str, bool] = {}
    for key, default in _DEFAULT_MEMORY.items():
        value = memory_value.get(key, default)
        if not isinstance(value, bool):
            raise _invalid_binding()
        memory[key] = value
    runtime["memory"] = memory
    return FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(
            primary=primary,
            fallbacks=fallbacks,
            retry_policy=retry_policy,
        ),
        system_prompt=system_prompt,
        prompt_version=prompt_version,
        runtime=runtime,
        knowledge_bindings=knowledge_bindings,
    )


def _parse_knowledge_bindings(
    retrieval: Mapping[str, Any], schema_version: int
) -> tuple[FrozenKnowledgeBinding, ...]:
    if schema_version == 1:
        values = retrieval.get("knowledge_snapshots", [])
        if not isinstance(values, list):
            raise _invalid_binding()
        bindings: list[FrozenKnowledgeBinding] = []
        for value in values:
            if not isinstance(value, Mapping) or not value.get("snapshot_hash"):
                raise _invalid_binding()
            try:
                snapshot_id = UUID(str(value["snapshot_id"]))
            except (KeyError, TypeError, ValueError):
                raise _invalid_binding() from None
            bindings.append(
                FrozenKnowledgeBinding(
                    knowledge_base_id=None,
                    binding_mode="PINNED",
                    snapshot_id=snapshot_id,
                    snapshot_hash=str(value["snapshot_hash"]),
                )
            )
        return tuple(bindings)

    values = retrieval.get("knowledge_bindings")
    if not isinstance(values, list):
        raise _invalid_binding()
    bindings: list[FrozenKnowledgeBinding] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise _invalid_binding()
        try:
            knowledge_base_id = UUID(str(value["knowledge_base_id"]))
            binding_mode = str(value["binding_mode"])
        except (KeyError, TypeError, ValueError):
            raise _invalid_binding() from None
        if binding_mode == "LATEST":
            if value.get("snapshot_id") is not None or value.get("snapshot_hash") is not None:
                raise _invalid_binding()
            bindings.append(FrozenKnowledgeBinding(knowledge_base_id, binding_mode))
            continue
        if binding_mode != "PINNED" or not value.get("snapshot_id") or not value.get(
            "snapshot_hash"
        ):
            raise _invalid_binding()
        try:
            snapshot_id = UUID(str(value["snapshot_id"]))
        except (TypeError, ValueError):
            raise _invalid_binding() from None
        bindings.append(
            FrozenKnowledgeBinding(
                knowledge_base_id,
                binding_mode,
                snapshot_id,
                str(value["snapshot_hash"]),
            )
        )
    return tuple(bindings)


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


__all__ = [
    "FrozenAgentSpec",
    "FrozenKnowledgeBinding",
    "SUPPORTED_SPEC_SCHEMA_VERSIONS",
    "parse_frozen_agent_spec",
]
