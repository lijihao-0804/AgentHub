"""Parsing and validation for immutable AgentVersion runtime projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.agent_runtime.context_policy import validate_frozen_context_policy
from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MEMORY_CONFIG,
    DEFAULT_RUNTIME_LIMITS,
    MAX_RUN_COST_LIMIT_MICRO_USD,
    MAX_RUNTIME_LIMITS,
)
from packages.core.errors.exceptions import AgentHubError
from packages.memory.selection_policy import SUPPORTED_MEMORY_SELECTION_POLICIES
from packages.model_gateway.capabilities import capabilities_from_mapping
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    ProviderEgress,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)

_DEFAULT_RUNTIME = DEFAULT_RUNTIME_LIMITS
_DEFAULT_CONTEXT_BUDGET = DEFAULT_CONTEXT_BUDGET
_DEFAULT_MEMORY = DEFAULT_MEMORY_CONFIG
SUPPORTED_SPEC_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5})
_SUMMARY_POLICY_VERSION = "thread_summary_policy_v1"
_SUMMARY_PROMPT_VERSION = "summary_candidate_zh_v1"
_SUMMARY_VALIDATOR_VERSION = "summary_mechanical_v1"
SUMMARY_REQUEST_STRATEGY_VERSION = "summary_request_strategy_v1"
SUMMARY_NON_THINKING_STRATEGY_VERSION = "summary_request_strategy_v2"
SUMMARY_NON_THINKING_MODE = "non_thinking"
SUMMARY_NON_THINKING_PROVIDER = "openai-compatible"
SUMMARY_NON_THINKING_MODEL = "deepseek-v4.1-flash-1017"
SUMMARY_NON_THINKING_ROUTE_ALLOWLIST = "aliyun_beijing_openai_compatible_v1"
SUMMARY_NON_THINKING_COUNTER_ID = "deepseek_v41_text_v1"
SUMMARY_NON_THINKING_COUNTER_REVISION = "9f9818127fdfbb60feba204799d617a874b32a86"
SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION = "0.22.2"
SUMMARY_NON_THINKING_TOKENIZER_SHA256 = (
    "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
)
_SUMMARY_POLICY_KEYS = frozenset(
    {
        "enabled",
        "policy_version",
        "prompt_version",
        "validator_version",
        "tokenizer_id",
        "tokenizer_version",
        "soft_trigger_percent",
        "hard_trigger_percent",
        "target_percent",
    }
)
_SUMMARY_NON_THINKING_KEYS = frozenset(
    {
        "request_strategy_version",
        "request_mode",
        "request_route_allowlist",
        "request_counter_id",
        "request_counter_revision",
        "request_counter_runtime_version",
        "request_counter_sha256",
        "request_counter_thinking",
    }
)
_SUMMARY_FIXED_VALUES = {
    "enabled": True,
    "policy_version": _SUMMARY_POLICY_VERSION,
    "prompt_version": _SUMMARY_PROMPT_VERSION,
    "validator_version": _SUMMARY_VALIDATOR_VERSION,
    "tokenizer_id": "approx_language_v1",
    "tokenizer_version": 1,
    "soft_trigger_percent": 70,
    "hard_trigger_percent": 85,
    "target_percent": 55,
}
_SUMMARY_NON_THINKING_VALUES = {
    "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
    "request_mode": SUMMARY_NON_THINKING_MODE,
    "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
    "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
    "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
    "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
    "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
    "request_counter_thinking": False,
}


@dataclass(frozen=True, slots=True)
class FrozenKnowledgeBinding:
    knowledge_base_id: UUID | None
    binding_mode: str
    snapshot_id: UUID | None = None
    snapshot_hash: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenWikiBinding:
    """The Wiki publication a version was published against.

    ``binding_mode`` records the original intent (PINNED or LATEST); the
    publication id and hash are the publish-time resolution itself, frozen
    once and never re-resolved when the version is read again.
    """

    knowledge_base_id: UUID
    binding_mode: str
    wiki_publication_id: UUID
    publication_hash: str


@dataclass(frozen=True, slots=True)
class FrozenAgentSpec:
    model_plan: ResolvedModelExecutionPlan
    system_prompt: str
    prompt_version: int
    runtime: dict[str, Any]
    knowledge_bindings: tuple[FrozenKnowledgeBinding, ...] = ()
    wiki_binding: FrozenWikiBinding | None = None


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
    wiki_binding = _parse_wiki_binding(resolved_spec)
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
    allowed_memory_keys = set(_DEFAULT_MEMORY) | {"max_memory_tokens", "selection_policy"}
    if schema_version in {3, 4, 5}:
        allowed_memory_keys.add("max_memory_tokens_source")
    if not set(memory_value).issubset(allowed_memory_keys):
        raise _invalid_binding()
    memory: dict[str, Any] = {}
    for key, default in _DEFAULT_MEMORY.items():
        value = memory_value.get(key, default)
        if not isinstance(value, bool):
            raise _invalid_binding()
        memory[key] = value
    max_memory_tokens = memory_value.get("max_memory_tokens")
    if max_memory_tokens is not None and (
        isinstance(max_memory_tokens, bool)
        or not isinstance(max_memory_tokens, int)
        or max_memory_tokens < 1
    ):
        raise _invalid_binding()
    if max_memory_tokens is not None and not memory["long_term_memory"]:
        raise _invalid_binding()
    if max_memory_tokens is not None:
        memory["max_memory_tokens"] = max_memory_tokens
    memory_source = memory_value.get("max_memory_tokens_source")
    if schema_version in {3, 4, 5} and memory["long_term_memory"]:
        if (
            not isinstance(memory_source, str)
            or memory_source not in {"UNSET", "DEFAULT", "CUSTOM"}
            or max_memory_tokens is None
        ):
            raise _invalid_binding()
        memory["max_memory_tokens_source"] = memory_source
    elif memory_source is not None:
        raise _invalid_binding()
    if "selection_policy" in memory_value:
        selection_policy = memory_value["selection_policy"]
        if (
            not memory["long_term_memory"]
            or not isinstance(selection_policy, str)
            or selection_policy not in SUPPORTED_MEMORY_SELECTION_POLICIES
        ):
            raise _invalid_binding()
        memory["selection_policy"] = selection_policy
    runtime["memory"] = memory
    policy_value = runtime_value.get("context_policy")
    if schema_version in {3, 4, 5}:
        sources = runtime_value.get("context_budget_sources")
        if (
            not isinstance(sources, Mapping)
            or set(sources) != set(_DEFAULT_CONTEXT_BUDGET)
            or any(
                not isinstance(source, str) or source not in {"UNSET", "DEFAULT", "CUSTOM"}
                for source in sources.values()
            )
        ):
            raise _invalid_binding()
        try:
            runtime["context_policy"] = validate_frozen_context_policy(
                policy_value,
                [model, *fallback_values],
                reserved_output_tokens=context_budget["reserved_output_tokens"],
            )
        except (TypeError, ValueError):
            raise _invalid_binding() from None
        runtime["context_budget_sources"] = dict(sources)
    elif policy_value is not None:
        raise _invalid_binding()
    history_policy = runtime_value.get("history_policy")
    if schema_version in {4, 5}:
        if history_policy != "deterministic_history_v1":
            raise _invalid_binding()
        runtime["history_policy"] = history_policy
    elif history_policy is not None:
        raise _invalid_binding()
    summary_policy = runtime_value.get("summary_policy")
    if schema_version == 5:
        runtime["summary_policy"] = _parse_summary_policy(summary_policy, primary=primary)
    elif summary_policy is not None:
        raise _invalid_binding()
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
        wiki_binding=wiki_binding,
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
        if (
            binding_mode != "PINNED"
            or not value.get("snapshot_id")
            or not value.get("snapshot_hash")
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


def _parse_summary_policy(
    value: Any,
    *,
    primary: ResolvedModelExecutionProfile | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid_binding()
    keys = frozenset(value)
    if keys not in {_SUMMARY_POLICY_KEYS, _SUMMARY_POLICY_KEYS | _SUMMARY_NON_THINKING_KEYS}:
        raise _invalid_binding()
    for key, expected in _SUMMARY_FIXED_VALUES.items():
        actual = value[key]
        if (
            isinstance(actual, bool)
            and not isinstance(expected, bool)
            or type(actual) is not type(expected)
            or actual != expected
        ):
            raise _invalid_binding()
    result = dict(_SUMMARY_FIXED_VALUES)
    if keys == _SUMMARY_POLICY_KEYS:
        return result
    if (
        primary is None
        or primary.provider != SUMMARY_NON_THINKING_PROVIDER
        or primary.model != SUMMARY_NON_THINKING_MODEL
    ):
        raise _invalid_binding()
    for key, expected in _SUMMARY_NON_THINKING_VALUES.items():
        actual = value[key]
        if (
            isinstance(actual, bool)
            and not isinstance(expected, bool)
            or type(actual) is not type(expected)
            or actual != expected
        ):
            raise _invalid_binding()
    result.update(_SUMMARY_NON_THINKING_VALUES)
    return result


def frozen_summary_request_identity(policy: Any) -> dict[str, Any]:
    """Return the server-owned strategy identity for a parsed summary policy."""
    if not isinstance(policy, Mapping):
        raise _invalid_binding()
    candidate_fields = frozenset(policy).intersection(_SUMMARY_NON_THINKING_KEYS)
    default_fields = frozenset({"request_strategy_version", "request_mode"})
    if not candidate_fields or candidate_fields == default_fields:
        if candidate_fields and (
            policy.get("request_strategy_version") != SUMMARY_REQUEST_STRATEGY_VERSION
            or policy.get("request_mode") != "provider_default"
        ):
            raise _invalid_binding()
        return {
            "request_strategy_version": SUMMARY_REQUEST_STRATEGY_VERSION,
            "request_mode": "provider_default",
        }
    if candidate_fields != _SUMMARY_NON_THINKING_KEYS or any(
        policy.get(key) != value
        or type(policy.get(key)) is not type(value)
        for key, value in _SUMMARY_NON_THINKING_VALUES.items()
    ):
        raise _invalid_binding()
    return dict(_SUMMARY_NON_THINKING_VALUES)


def _parse_wiki_binding(resolved_spec: Mapping[str, Any]) -> FrozenWikiBinding | None:
    value = resolved_spec.get("wiki_binding")
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise _invalid_binding()
    binding_mode = value.get("binding_mode")
    # A published spec always freezes the resolved publication, whatever the
    # original intent was: an unresolved LATEST section would be a binding the
    # runtime cannot honour, so it is rejected rather than silently skipped.
    if binding_mode not in {"PINNED", "LATEST"} or not value.get("publication_hash"):
        raise _invalid_binding()
    try:
        knowledge_base_id = UUID(str(value["knowledge_base_id"]))
        wiki_publication_id = UUID(str(value["wiki_publication_id"]))
    except (KeyError, TypeError, ValueError):
        raise _invalid_binding() from None
    return FrozenWikiBinding(
        knowledge_base_id=knowledge_base_id,
        binding_mode=str(binding_mode),
        wiki_publication_id=wiki_publication_id,
        publication_hash=str(value["publication_hash"]),
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
        provider_egress = None
        # Presence of this mapping selects a versioned egress policy even when
        # its base_url is null. Policy v1 resolves null from fixed builtins for
        # the frozen provider; absence remains the pre-policy legacy state.
        if "provider_egress" in value:
            raw_provider_egress = value["provider_egress"]
            if (
                not isinstance(raw_provider_egress, Mapping)
                or set(raw_provider_egress) != {"policy_version", "base_url"}
                or raw_provider_egress.get("policy_version") != PROVIDER_EGRESS_POLICY_VERSION
            ):
                raise ValueError
            provider_egress = ProviderEgress(
                policy_version=raw_provider_egress["policy_version"],
                base_url=raw_provider_egress["base_url"],
            )
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
        provider_egress=provider_egress,
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
    "FrozenWikiBinding",
    "SUMMARY_NON_THINKING_COUNTER_ID",
    "SUMMARY_NON_THINKING_COUNTER_REVISION",
    "SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION",
    "SUMMARY_NON_THINKING_MODE",
    "SUMMARY_NON_THINKING_MODEL",
    "SUMMARY_NON_THINKING_PROVIDER",
    "SUMMARY_NON_THINKING_ROUTE_ALLOWLIST",
    "SUMMARY_NON_THINKING_STRATEGY_VERSION",
    "SUMMARY_NON_THINKING_TOKENIZER_SHA256",
    "SUMMARY_REQUEST_STRATEGY_VERSION",
    "SUPPORTED_SPEC_SCHEMA_VERSIONS",
    "frozen_summary_request_identity",
    "parse_frozen_agent_spec",
]
