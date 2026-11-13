"""Pure, deny-by-default support checks for interactive case budgeting.

This module decides only whether a case is in the first supported subset.  A
candidate is not dispatch authorization: the caller must still supply a
server-owned ``BillingAttestation``, persist the case/run ledger records, and
pass the live dispatch fence.

The first pricing integration rejects every model request that contains tools,
including builtin read tools.  The admission matrix therefore rejects every
published or dynamically supplied tool rather than claiming that
``query_customer`` is supported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from packages.agent_runtime.frozen import FrozenAgentSpec, parse_frozen_agent_spec
from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MEMORY_CONFIG,
    DEFAULT_RUNTIME_LIMITS,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.interactive_protocol import (
    INTERACTIVE_PROTOCOL_KEY,
    require_interactive_protocol,
)
from packages.evaluation.judge import JUDGE_DISABLED_VERSION, JUDGE_MANIFEST_KEY
from packages.evaluation.reproducibility import (
    DEFAULT_EVALUATOR_VERSIONS,
    EVALUATION_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
)
from packages.model_gateway.contracts import ProviderEgress
from packages.tools.contracts import ToolDefinition, ToolEffect, ToolSourceKind

INTERACTIVE_CASE_BUDGET_ADMISSION_VERSION = "interactive-case-budget-admission-v1"
_INTERACTIVE_CATEGORY = "INTERACTIVE"
_SUPPORT_ENVIRONMENT = "support_customer"
_SCRIPTED_SIMULATOR_VERSION = "scripted-disclosure-v1"
_GENERATE_REQUEST_MODE = "generate"
_MAX_CASE_BUDGET_CNY = Decimal("999999999999.99999999")
_MONEY_SCALE = 8

_INPUT_KEYS = frozenset(
    {
        "task",
        "environment_ref",
        "simulator",
        "seed",
        "max_turns",
        "max_tokens",
        "max_cost_cny",
    }
)
_REQUIRED_INPUT_KEYS = frozenset({"task", "environment_ref", "simulator"})
_SIMULATOR_KEYS = frozenset({"private_facts", "triggers", "fallback", "max_turns"})
_REQUIRED_SIMULATOR_KEYS = frozenset({"private_facts", "triggers"})
_EXPECTED_KEYS = frozenset(
    {
        "assertions",
        "canary",
        "approval_decisions",
        "required_tools",
        "forbidden_tools",
    }
)
_REQUIRED_EXPECTED_KEYS = frozenset({"assertions"})

_MANIFEST_KEYS = frozenset(
    {
        "evaluation_schema_version",
        "observation_schema_version",
        "evaluator_versions",
        INTERACTIVE_PROTOCOL_KEY,
    }
)

_SPEC_KEYS = frozenset(
    {
        "spec_schema_version",
        "model",
        "prompt",
        "retrieval",
        "tools",
        "runtime",
        "wiki_binding",
    }
)
_REQUIRED_SPEC_KEYS = frozenset(
    {"spec_schema_version", "model", "prompt", "retrieval", "tools", "runtime"}
)
_MODEL_KEYS = frozenset(
    {
        "profile_id",
        "credential_ref",
        "provider",
        "model",
        "temperature",
        "max_tokens",
        "timeout_seconds",
        "capabilities",
        "retry_policy",
        "fallback_chain",
        "fallback_profiles",
        "provider_egress",
    }
)
_CAPABILITY_KEYS = frozenset(
    {
        "tool_calling",
        "streaming",
        "structured_output",
        "vision",
        "max_context_tokens",
        "context_metadata",
    }
)
_RETRIEVAL_KEYS = frozenset(
    {
        "embedding_model",
        "reranker_model",
        "retrieval_strategy",
        "dense_top_k",
        "sparse_top_k",
        "candidate_top_k",
        "final_top_k",
        "rerank_top_k",
        "max_chunks_per_document",
        "context_budget_bytes",
        "evidence_projection",
        "evidence_set_selector",
        "knowledge_binding_mode",
        "knowledge_snapshot_ids",
        "knowledge_snapshots",
        "knowledge_bindings",
    }
)
_RUNTIME_KEYS = (
    frozenset(DEFAULT_RUNTIME_LIMITS)
    | frozenset(DEFAULT_CONTEXT_BUDGET)
    | frozenset(DEFAULT_MEMORY_CONFIG)
    | {
        "max_cost_micro_usd",
        "context_budget",
        "memory",
        "context_policy",
        "context_budget_sources",
        "history_policy",
        "summary_policy",
    }
)
_MEMORY_KEYS = frozenset(
    {
        *DEFAULT_MEMORY_CONFIG,
        "max_memory_tokens",
        "max_memory_tokens_source",
        "selection_policy",
    }
)
_WIKI_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "knowledge_base_id",
        "binding_mode",
        "wiki_publication_id",
        "publication_hash",
    }
)


class CaseBudgetAdmissionReason(StrEnum):
    """Stable, content-free reasons for a support-matrix refusal."""

    CATEGORY_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_CATEGORY_UNSUPPORTED"
    INPUT_CONTRACT_INVALID = "INTERACTIVE_CASE_BUDGET_INPUT_CONTRACT_INVALID"
    ENVIRONMENT_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_ENVIRONMENT_UNSUPPORTED"
    CASE_CAP_MISSING = "INTERACTIVE_CASE_BUDGET_CAP_MISSING"
    CASE_CAP_INVALID = "INTERACTIVE_CASE_BUDGET_CAP_INVALID"
    INTERACTIVE_PROTOCOL_NOT_FROZEN = "INTERACTIVE_PROTOCOL_NOT_FROZEN"
    MANIFEST_INCOMPLETE = "INTERACTIVE_CASE_BUDGET_MANIFEST_INCOMPLETE"
    MANIFEST_UNKNOWN = "INTERACTIVE_CASE_BUDGET_MANIFEST_UNKNOWN"
    MANIFEST_VERSION_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_MANIFEST_VERSION_UNSUPPORTED"
    JUDGE_ENSEMBLE_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_JUDGE_ENSEMBLE_UNSUPPORTED"
    FROZEN_SPEC_INVALID = "INTERACTIVE_CASE_BUDGET_FROZEN_SPEC_INVALID"
    FROZEN_SPEC_UNKNOWN = "INTERACTIVE_CASE_BUDGET_FROZEN_SPEC_UNKNOWN"
    FROZEN_SPEC_MISMATCH = "INTERACTIVE_CASE_BUDGET_FROZEN_SPEC_MISMATCH"
    PROVIDER_EGRESS_REQUIRED = "INTERACTIVE_CASE_BUDGET_PROVIDER_EGRESS_REQUIRED"
    MODEL_CHAIN_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_MODEL_CHAIN_UNSUPPORTED"
    MODEL_CAPABILITIES_UNKNOWN = "INTERACTIVE_CASE_BUDGET_MODEL_CAPABILITIES_UNKNOWN"
    TEXT_GENERATE_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_TEXT_GENERATE_UNSUPPORTED"
    SUMMARY_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_SUMMARY_UNSUPPORTED"
    OPEN_ENDED_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_OPEN_ENDED_UNSUPPORTED"
    PAID_MEMORY_WRITER_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_PAID_MEMORY_WRITER_UNSUPPORTED"
    TOOLS_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_TOOLS_UNSUPPORTED"
    TOOL_EVIDENCE_INVALID = "INTERACTIVE_CASE_BUDGET_TOOL_EVIDENCE_INVALID"
    RETRIEVAL_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_RETRIEVAL_UNSUPPORTED"
    APPROVAL_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_APPROVAL_UNSUPPORTED"
    REQUEST_MODE_UNSUPPORTED = "INTERACTIVE_CASE_BUDGET_REQUEST_MODE_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class CaseBudgetAdmissionRejection:
    """A typed refusal carrying only a stable machine-readable reason."""

    reason: CaseBudgetAdmissionReason

    @property
    def code(self) -> str:
        return self.reason.value

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class InteractiveCaseBudgetCandidate:
    """Immutable evidence for the supported admission subset.

    Constructing this value does not authorize a provider, tool, judge, or
    memory-writer dispatch.
    """

    workspace_id: UUID
    resolved_spec_hash: str
    max_cost_cny: Decimal
    max_turns: int
    interactive_max_tokens: int | None
    primary_profile_id: UUID
    provider_credential_id: UUID
    provider: str
    model: str
    provider_egress: ProviderEgress
    max_context_tokens: int
    max_output_tokens: int
    category: Literal["INTERACTIVE"] = "INTERACTIVE"
    environment_ref: Literal["support_customer"] = "support_customer"
    simulator_version: Literal["scripted-disclosure-v1"] = "scripted-disclosure-v1"
    request_mode: Literal["generate"] = "generate"
    currency: Literal["CNY"] = "CNY"
    budget_policy_version: Literal["interactive_case_budget_v1"] = "interactive_case_budget_v1"

    @property
    def dispatch_authorized(self) -> Literal[False]:
        """Return false until external attestation, ledger, and fence succeed."""

        return False


type CaseBudgetAdmissionResult = InteractiveCaseBudgetCandidate | CaseBudgetAdmissionRejection


def validate_interactive_case_budget_support(
    *,
    category: str,
    input_contract: Mapping[str, Any],
    expected: Mapping[str, Any],
    evaluator_manifest: Mapping[str, Any] | None,
    resolved_spec: Mapping[str, Any] | None,
    frozen_spec: FrozenAgentSpec | None,
    resolved_tools: Sequence[ToolDefinition] | None,
    memory_writer: object | None,
    request_mode: str,
) -> CaseBudgetAdmissionResult:
    """Validate the first interactive case-budget support subset.

    Every decision comes from the item contract, the complete evaluator
    manifest, the frozen resolved spec, and server-supplied runtime evidence.
    Caller-provided support booleans are not accepted.  Unknown or incomplete
    evidence produces a typed refusal.
    """

    if category != _INTERACTIVE_CATEGORY:
        return _reject(CaseBudgetAdmissionReason.CATEGORY_UNSUPPORTED)

    manifest_reason = _manifest_rejection(evaluator_manifest)
    if manifest_reason is not None:
        return _reject(manifest_reason)

    input_result = _validated_input(input_contract, expected)
    if isinstance(input_result, CaseBudgetAdmissionRejection):
        return input_result

    spec_result = _validated_frozen_spec(resolved_spec, frozen_spec)
    if isinstance(spec_result, CaseBudgetAdmissionRejection):
        return spec_result

    if memory_writer is not None:
        return _reject(CaseBudgetAdmissionReason.PAID_MEMORY_WRITER_UNSUPPORTED)

    tool_reason = _tool_rejection(resolved_tools)
    if tool_reason is not None:
        return _reject(tool_reason)

    if request_mode != _GENERATE_REQUEST_MODE:
        return _reject(CaseBudgetAdmissionReason.REQUEST_MODE_UNSUPPORTED)

    primary = spec_result.model_plan.primary
    return InteractiveCaseBudgetCandidate(
        workspace_id=primary.workspace_id,
        resolved_spec_hash=canonical_json_hash(dict(resolved_spec)),
        max_cost_cny=input_result.max_cost_cny,
        max_turns=input_result.max_turns,
        interactive_max_tokens=input_result.max_tokens,
        primary_profile_id=primary.id,
        provider_credential_id=primary.provider_credential_id,
        provider=primary.provider,
        model=primary.model,
        provider_egress=primary.provider_egress,
        max_context_tokens=primary.capabilities.max_context_tokens,
        max_output_tokens=primary.max_tokens,
    )


@dataclass(frozen=True, slots=True)
class _ValidatedInput:
    max_cost_cny: Decimal
    max_turns: int
    max_tokens: int | None


def _validated_input(
    input_contract: object,
    expected: object,
) -> _ValidatedInput | CaseBudgetAdmissionRejection:
    if not isinstance(input_contract, Mapping) or not isinstance(expected, Mapping):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    input_keys = frozenset(input_contract)
    expected_keys = frozenset(expected)
    if (
        not _REQUIRED_INPUT_KEYS.issubset(input_keys)
        or not input_keys.issubset(_INPUT_KEYS)
        or not _REQUIRED_EXPECTED_KEYS.issubset(expected_keys)
    ):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    if expected.get("open_ended") is True:
        return _reject(CaseBudgetAdmissionReason.OPEN_ENDED_UNSUPPORTED)
    if not expected_keys.issubset(_EXPECTED_KEYS):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)

    task = input_contract.get("task")
    if not isinstance(task, str) or not task.strip():
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    if input_contract.get("environment_ref") != _SUPPORT_ENVIRONMENT:
        return _reject(CaseBudgetAdmissionReason.ENVIRONMENT_UNSUPPORTED)

    simulator = input_contract.get("simulator")
    if not isinstance(simulator, Mapping):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    simulator_keys = frozenset(simulator)
    if (
        not _REQUIRED_SIMULATOR_KEYS.issubset(simulator_keys)
        or not simulator_keys.issubset(_SIMULATOR_KEYS)
        or not isinstance(simulator.get("private_facts"), Mapping)
        or not isinstance(simulator.get("triggers"), Mapping)
    ):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    fallback = simulator.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)

    seed = input_contract.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    turn_limits = (
        input_contract.get("max_turns", simulator.get("max_turns", 4)),
        simulator.get("max_turns", 4),
    )
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10
        for limit in turn_limits
    ):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)

    max_tokens = input_contract.get("max_tokens")
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0
    ):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)

    if "max_cost_cny" not in input_contract:
        return _reject(CaseBudgetAdmissionReason.CASE_CAP_MISSING)
    cap = _parse_cny_cap(input_contract.get("max_cost_cny"))
    if cap is None:
        return _reject(CaseBudgetAdmissionReason.CASE_CAP_INVALID)

    assertions = expected.get("assertions")
    if not isinstance(assertions, Mapping):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    canary = expected.get("canary")
    if canary is not None and not isinstance(canary, str):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)

    approval_decisions = expected.get("approval_decisions", [])
    if not isinstance(approval_decisions, list) or any(
        decision not in {"APPROVED", "DENIED"} for decision in approval_decisions
    ):
        return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
    if approval_decisions:
        return _reject(CaseBudgetAdmissionReason.APPROVAL_UNSUPPORTED)

    for key in ("required_tools", "forbidden_tools"):
        identities = expected.get(key, [])
        if not isinstance(identities, list) or any(
            not isinstance(identity, str) or not identity for identity in identities
        ):
            return _reject(CaseBudgetAdmissionReason.INPUT_CONTRACT_INVALID)
        if identities:
            return _reject(CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED)

    return _ValidatedInput(
        max_cost_cny=cap,
        max_turns=min(turn_limits),
        max_tokens=max_tokens,
    )


def _parse_cny_cap(value: object) -> Decimal | None:
    if not isinstance(value, (Decimal, str)):
        return None
    if isinstance(value, str) and (
        not value or value != value.strip() or "\x00" in value or len(value) > 64
    ):
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (
        not amount.is_finite()
        or amount <= 0
        or amount > _MAX_CASE_BUDGET_CNY
        or amount.as_tuple().exponent < -_MONEY_SCALE
    ):
        return None
    return amount


def _manifest_rejection(
    manifest: Mapping[str, Any] | None,
) -> CaseBudgetAdmissionReason | None:
    try:
        require_interactive_protocol(manifest)
    except Exception:
        return CaseBudgetAdmissionReason.INTERACTIVE_PROTOCOL_NOT_FROZEN
    protocol = manifest.get(INTERACTIVE_PROTOCOL_KEY)
    if (
        not isinstance(protocol, Mapping)
        or protocol.get("simulator_version") != _SCRIPTED_SIMULATOR_VERSION
    ):
        return CaseBudgetAdmissionReason.INTERACTIVE_PROTOCOL_NOT_FROZEN

    versions = manifest.get("evaluator_versions")
    judge_present = (
        "judge_profile" in manifest
        or "judge_ensemble" in manifest
        or (
            isinstance(versions, Mapping)
            and versions.get(JUDGE_MANIFEST_KEY, JUDGE_DISABLED_VERSION) != JUDGE_DISABLED_VERSION
        )
    )
    if judge_present:
        return CaseBudgetAdmissionReason.JUDGE_ENSEMBLE_UNSUPPORTED

    manifest_keys = frozenset(manifest)
    if not _MANIFEST_KEYS.issubset(manifest_keys):
        return CaseBudgetAdmissionReason.MANIFEST_INCOMPLETE
    if not manifest_keys.issubset(_MANIFEST_KEYS):
        return CaseBudgetAdmissionReason.MANIFEST_UNKNOWN
    if not isinstance(versions, Mapping):
        return CaseBudgetAdmissionReason.MANIFEST_INCOMPLETE
    expected_versions = dict(DEFAULT_EVALUATOR_VERSIONS)
    version_keys = frozenset(versions)
    expected_version_keys = frozenset(expected_versions)
    if not expected_version_keys.issubset(version_keys):
        return CaseBudgetAdmissionReason.MANIFEST_INCOMPLETE
    if not version_keys.issubset(expected_version_keys):
        return CaseBudgetAdmissionReason.MANIFEST_UNKNOWN
    if (
        manifest.get("evaluation_schema_version") != EVALUATION_SCHEMA_VERSION
        or manifest.get("observation_schema_version") != OBSERVATION_SCHEMA_VERSION
        or any(versions.get(key) != value for key, value in expected_versions.items())
    ):
        return CaseBudgetAdmissionReason.MANIFEST_VERSION_UNSUPPORTED
    return None


def _validated_frozen_spec(
    resolved_spec: Mapping[str, Any] | None,
    frozen_spec: FrozenAgentSpec | None,
) -> FrozenAgentSpec | CaseBudgetAdmissionRejection:
    if not isinstance(resolved_spec, Mapping) or not isinstance(frozen_spec, FrozenAgentSpec):
        return _reject(CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID)

    shape_reason = _spec_shape_rejection(resolved_spec)
    if shape_reason is not None:
        return _reject(shape_reason)

    tools = resolved_spec["tools"]
    retrieval = resolved_spec["retrieval"]
    if not isinstance(tools, list):
        return _reject(CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID)
    if tools:
        return _reject(CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED)

    try:
        workspace_id = frozen_spec.model_plan.primary.workspace_id
        parsed = parse_frozen_agent_spec(resolved_spec, workspace_id=workspace_id)
        canonical_json_hash(dict(resolved_spec))
        for candidate_spec in (frozen_spec, parsed):
            semantic_reason = _frozen_spec_semantic_rejection(
                candidate_spec,
                raw_retrieval=retrieval,
            )
            if semantic_reason is not None:
                return _reject(semantic_reason)
    except Exception:
        return _reject(CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID)

    if parsed != frozen_spec:
        return _reject(CaseBudgetAdmissionReason.FROZEN_SPEC_MISMATCH)
    return parsed


def _spec_shape_rejection(
    spec: Mapping[str, Any],
) -> CaseBudgetAdmissionReason | None:
    keys = frozenset(spec)
    if not _REQUIRED_SPEC_KEYS.issubset(keys):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if not keys.issubset(_SPEC_KEYS):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN

    model = spec.get("model")
    if not isinstance(model, Mapping):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    model_keys = frozenset(model)
    if "provider_egress" not in model_keys:
        return CaseBudgetAdmissionReason.PROVIDER_EGRESS_REQUIRED
    if not _MODEL_KEYS.issubset(model_keys):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if not model_keys.issubset(_MODEL_KEYS):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN

    capabilities = model.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if not frozenset(capabilities).issubset(_CAPABILITY_KEYS):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN

    retry_policy = model.get("retry_policy")
    if not isinstance(retry_policy, Mapping):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if frozenset(retry_policy) != {"max_attempts"}:
        return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN

    fallback_profiles = model.get("fallback_profiles")
    fallback_chain = model.get("fallback_chain")
    if not isinstance(fallback_profiles, list) or not isinstance(fallback_chain, list):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if fallback_profiles or fallback_chain:
        return CaseBudgetAdmissionReason.MODEL_CHAIN_UNSUPPORTED
    retry_attempts = retry_policy.get("max_attempts")
    if type(retry_attempts) is not int or retry_attempts != 1:
        return CaseBudgetAdmissionReason.MODEL_CHAIN_UNSUPPORTED

    prompt = spec.get("prompt")
    if not isinstance(prompt, Mapping):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    prompt_keys = frozenset(prompt)
    if prompt_keys not in {
        frozenset({"system_prompt", "prompt_version"}),
        frozenset({"system", "version"}),
    }:
        return (
            CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN
            if prompt_keys.issuperset({"system_prompt", "prompt_version"})
            or prompt_keys.issuperset({"system", "version"})
            else CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
        )

    retrieval = spec.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if not frozenset(retrieval).issubset(_RETRIEVAL_KEYS):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN

    runtime = spec.get("runtime")
    if not isinstance(runtime, Mapping):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if not frozenset(runtime).issubset(_RUNTIME_KEYS):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN
    memory = runtime.get("memory")
    if memory is not None:
        if not isinstance(memory, Mapping):
            return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
        if not frozenset(memory).issubset(_MEMORY_KEYS):
            return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN

    wiki_binding = spec.get("wiki_binding")
    if wiki_binding is not None:
        if not isinstance(wiki_binding, Mapping):
            return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
        if not frozenset(wiki_binding).issubset(_WIKI_BINDING_KEYS):
            return CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN
    return None


def _frozen_spec_semantic_rejection(
    spec: FrozenAgentSpec,
    *,
    raw_retrieval: Mapping[str, Any],
) -> CaseBudgetAdmissionReason | None:
    plan = spec.model_plan
    if (
        not isinstance(plan.primary.id, UUID)
        or not isinstance(plan.primary.workspace_id, UUID)
        or not isinstance(plan.primary.provider_credential_id, UUID)
        or not isinstance(plan.primary.provider, str)
        or not plan.primary.provider
        or not isinstance(plan.primary.model, str)
        or not plan.primary.model
    ):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    if plan.fallbacks or plan.retry_policy.max_attempts != 1:
        return CaseBudgetAdmissionReason.MODEL_CHAIN_UNSUPPORTED
    provider_egress = plan.primary.provider_egress
    if provider_egress is None:
        return CaseBudgetAdmissionReason.PROVIDER_EGRESS_REQUIRED
    if not isinstance(provider_egress, ProviderEgress):
        return CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID
    context_tokens = plan.primary.capabilities.max_context_tokens
    if (
        isinstance(context_tokens, bool)
        or not isinstance(context_tokens, int)
        or context_tokens <= 0
        or isinstance(plan.primary.max_tokens, bool)
        or not isinstance(plan.primary.max_tokens, int)
        or plan.primary.max_tokens <= 0
    ):
        return CaseBudgetAdmissionReason.MODEL_CAPABILITIES_UNKNOWN
    if plan.primary.capabilities.vision:
        return CaseBudgetAdmissionReason.TEXT_GENERATE_UNSUPPORTED

    summary_policy = spec.runtime.get("summary_policy")
    if summary_policy is not None:
        if not isinstance(summary_policy, Mapping):
            return CaseBudgetAdmissionReason.SUMMARY_UNSUPPORTED
        if summary_policy and summary_policy.get("enabled") is not False:
            return CaseBudgetAdmissionReason.SUMMARY_UNSUPPORTED

    memory = spec.runtime.get("memory")
    if isinstance(memory, Mapping) and memory.get("thread_history_search") is True:
        return CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED
    if (
        spec.knowledge_bindings
        or spec.wiki_binding is not None
        or any(raw_retrieval.get(key) for key in _RETRIEVAL_BINDING_KEYS)
        or raw_retrieval.get("evidence_set_selector") is not None
    ):
        return CaseBudgetAdmissionReason.RETRIEVAL_UNSUPPORTED
    return None


_RETRIEVAL_BINDING_KEYS = (
    "knowledge_snapshot_ids",
    "knowledge_snapshots",
    "knowledge_bindings",
)


def _tool_rejection(
    resolved_tools: Sequence[ToolDefinition] | None,
) -> CaseBudgetAdmissionReason | None:
    if (
        not isinstance(resolved_tools, Sequence)
        or isinstance(resolved_tools, (str, bytes))
        or any(
            not isinstance(tool, ToolDefinition)
            or not isinstance(tool.source_kind, ToolSourceKind)
            or not isinstance(tool.effect, ToolEffect)
            for tool in resolved_tools
        )
    ):
        return CaseBudgetAdmissionReason.TOOL_EVIDENCE_INVALID
    if resolved_tools:
        return CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED
    return None


def _reject(reason: CaseBudgetAdmissionReason) -> CaseBudgetAdmissionRejection:
    return CaseBudgetAdmissionRejection(reason=reason)


__all__ = [
    "CaseBudgetAdmissionReason",
    "CaseBudgetAdmissionRejection",
    "CaseBudgetAdmissionResult",
    "INTERACTIVE_CASE_BUDGET_ADMISSION_VERSION",
    "InteractiveCaseBudgetCandidate",
    "validate_interactive_case_budget_support",
]
