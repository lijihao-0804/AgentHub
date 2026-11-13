from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from packages.agent_runtime.frozen import FrozenAgentSpec, parse_frozen_agent_spec
from packages.agent_runtime.models import Agent
from packages.agent_runtime.publish import _resolved_spec
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.case_budget_admission import (
    CaseBudgetAdmissionReason,
    CaseBudgetAdmissionRejection,
    InteractiveCaseBudgetCandidate,
    validate_interactive_case_budget_support,
)
from packages.evaluation.interactive_protocol import (
    INTERACTIVE_PROTOCOL_KEY,
    interactive_protocol_manifest,
)
from packages.evaluation.reproducibility import default_evaluator_manifest
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    ModelCapabilities,
    ProviderEgress,
)
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
    ToolSourceKind,
)

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
CREDENTIAL_ID = UUID("00000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("00000000-0000-0000-0000-000000000004")


def _egress(base_url: str | None) -> ProviderEgress:
    return ProviderEgress(
        policy_version=PROVIDER_EGRESS_POLICY_VERSION,
        base_url=base_url,
    )


def _raw_spec(
    *,
    base_url: str | None = "https://provider.example/v1",
    with_egress: bool = True,
) -> dict[str, Any]:
    profile = ResolvedModelProfile(
        id=PROFILE_ID,
        workspace_id=WORKSPACE_ID,
        provider_credential_id=CREDENTIAL_ID,
        model="synthetic-text-model",
        temperature=Decimal("0"),
        max_tokens=800,
        timeout_seconds=Decimal("30"),
        fallback_profile_id=None,
        capabilities=ModelCapabilities(
            tool_calling=False,
            streaming=True,
            structured_output=True,
            vision=False,
            max_context_tokens=16_000,
        ),
    )
    providers = {PROFILE_ID: "synthetic-provider"}
    model: Any = (profile,), providers
    if with_egress:
        model = (profile,), providers, {PROFILE_ID: _egress(base_url)}
    agent = Agent(
        id=CREDENTIAL_ID,
        workspace_id=WORKSPACE_ID,
        name="Interactive budget admission",
        system_prompt="Synthetic support prompt.",
        prompt_version=1,
        model_profile_id=PROFILE_ID,
        knowledge_binding_mode="PINNED",
        model_retry_policy={"max_attempts": 1},
        retrieval_config={},
        runtime_config={},
    )
    return _resolved_spec(
        agent=agent,
        model=model,
        bindings=(),
        tools=(),
    )


def _legacy_raw_spec() -> dict[str, Any]:
    return _raw_spec(with_egress=False)


def _input_contract() -> dict[str, Any]:
    return {
        "task": "Resolve the synthetic support request.",
        "environment_ref": "support_customer",
        "simulator": {
            "private_facts": {"customer_ref": "__CUSTOMER_REF__"},
            "triggers": {"customer_ref": ["customer reference"]},
            "fallback": "I do not have more information.",
            "max_turns": 3,
        },
        "max_turns": 3,
        "max_tokens": 4_096,
        "max_cost_cny": Decimal("12.34000000"),
    }


def _expected() -> dict[str, Any]:
    return {"assertions": {"ticket_count": 0}}


def _manifest() -> dict[str, Any]:
    return {
        **default_evaluator_manifest(),
        INTERACTIVE_PROTOCOL_KEY: interactive_protocol_manifest(),
    }


def _frozen(raw: dict[str, Any] | None = None) -> FrozenAgentSpec:
    return parse_frozen_agent_spec(raw or _raw_spec(), workspace_id=WORKSPACE_ID)


def _call(**overrides: Any) -> dict[str, Any]:
    raw = overrides.pop("resolved_spec", _raw_spec())
    frozen = overrides.pop("frozen_spec", None)
    return {
        "category": "INTERACTIVE",
        "input_contract": _input_contract(),
        "expected": _expected(),
        "evaluator_manifest": _manifest(),
        "resolved_spec": raw,
        "frozen_spec": _frozen(raw) if frozen is None else frozen,
        "resolved_tools": (),
        "memory_writer": None,
        "request_mode": "generate",
        **overrides,
    }


def _validate(**overrides: Any):
    return validate_interactive_case_budget_support(**_call(**overrides))


def _assert_refused(result: object, reason: CaseBudgetAdmissionReason) -> None:
    assert isinstance(result, CaseBudgetAdmissionRejection)
    assert result.reason is reason
    assert str(result) == reason.value


def _query_customer_tool() -> ToolDefinition:
    return ToolDefinition(
        identity="query_customer",
        revision_id=REVISION_ID,
        spec_hash="a" * 64,
        description="Synthetic builtin read.",
        input_schema={"type": "object"},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=10,
        source_kind=ToolSourceKind.BUILTIN,
    )


def test_supported_subset_returns_immutable_non_dispatch_candidate() -> None:
    raw = _raw_spec()
    result = _validate(resolved_spec=raw)

    assert isinstance(result, InteractiveCaseBudgetCandidate)
    assert result.workspace_id == WORKSPACE_ID
    assert result.resolved_spec_hash == canonical_json_hash(raw)
    assert result.max_cost_cny == Decimal("12.34000000")
    assert result.max_turns == 3
    assert result.interactive_max_tokens == 4_096
    assert result.primary_profile_id == PROFILE_ID
    assert result.provider_credential_id == CREDENTIAL_ID
    assert result.provider == "synthetic-provider"
    assert result.model == "synthetic-text-model"
    assert raw["model"]["provider_egress"] == {
        "policy_version": PROVIDER_EGRESS_POLICY_VERSION,
        "base_url": "https://provider.example/v1",
    }
    assert result.provider_egress == _egress("https://provider.example/v1")
    assert result.max_context_tokens == 16_000
    assert result.max_output_tokens == 800
    assert result.category == "INTERACTIVE"
    assert result.environment_ref == "support_customer"
    assert result.simulator_version == "scripted-disclosure-v1"
    assert result.request_mode == "generate"
    assert result.currency == "CNY"
    assert result.budget_policy_version == "interactive_case_budget_v1"
    assert result.dispatch_authorized is False
    with pytest.raises(FrozenInstanceError):
        result.max_cost_cny = Decimal("1")  # type: ignore[misc]


def test_current_published_shape_accepts_explicit_null_builtin_route() -> None:
    raw = _raw_spec(base_url=None)
    result = _validate(resolved_spec=raw)

    assert raw["model"]["provider_egress"] == {
        "policy_version": PROVIDER_EGRESS_POLICY_VERSION,
        "base_url": None,
    }
    assert isinstance(result, InteractiveCaseBudgetCandidate)
    assert result.provider_egress == _egress(None)


def test_legacy_published_shape_without_egress_is_rejected() -> None:
    raw = _legacy_raw_spec()
    frozen = parse_frozen_agent_spec(raw, workspace_id=WORKSPACE_ID)

    assert "provider_egress" not in raw["model"]
    assert frozen.model_plan.primary.provider_egress is None
    _assert_refused(
        _validate(resolved_spec=raw, frozen_spec=frozen),
        CaseBudgetAdmissionReason.PROVIDER_EGRESS_REQUIRED,
    )


def test_unknown_provider_egress_policy_is_rejected_by_frozen_parser() -> None:
    raw = _raw_spec()
    raw["model"]["provider_egress"]["policy_version"] = "provider_egress_v2"

    with pytest.raises(AgentHubError) as raised:
        parse_frozen_agent_spec(raw, workspace_id=WORKSPACE_ID)

    assert raised.value.code == "AGENT_VERSION_MODEL_BINDING_INVALID"
    _assert_refused(
        _validate(resolved_spec=raw, frozen_spec=_frozen()),
        CaseBudgetAdmissionReason.FROZEN_SPEC_INVALID,
    )


def test_category_environment_and_missing_cap_are_typed_refusals() -> None:
    _assert_refused(
        _validate(category="TOOL"),
        CaseBudgetAdmissionReason.CATEGORY_UNSUPPORTED,
    )

    wrong_environment = _input_contract()
    wrong_environment["environment_ref"] = "remote_support"
    _assert_refused(
        _validate(input_contract=wrong_environment),
        CaseBudgetAdmissionReason.ENVIRONMENT_UNSUPPORTED,
    )

    missing_cap = _input_contract()
    missing_cap.pop("max_cost_cny")
    _assert_refused(
        _validate(input_contract=missing_cap),
        CaseBudgetAdmissionReason.CASE_CAP_MISSING,
    )


@pytest.mark.parametrize(
    "cap",
    [
        None,
        True,
        1.0,
        1,
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("0.000000001"),
        Decimal("1000000000000"),
        "SECRET-NOT-A-DECIMAL",
    ],
)
def test_invalid_caps_never_echo_input(cap: object) -> None:
    input_contract = _input_contract()
    input_contract["max_cost_cny"] = cap
    result = _validate(input_contract=input_contract)

    _assert_refused(result, CaseBudgetAdmissionReason.CASE_CAP_INVALID)
    assert "SECRET" not in repr(result)


def test_protocol_and_complete_manifest_are_required() -> None:
    _assert_refused(
        _validate(evaluator_manifest={}),
        CaseBudgetAdmissionReason.INTERACTIVE_PROTOCOL_NOT_FROZEN,
    )

    protocol_only = {INTERACTIVE_PROTOCOL_KEY: interactive_protocol_manifest()}
    _assert_refused(
        _validate(evaluator_manifest=protocol_only),
        CaseBudgetAdmissionReason.MANIFEST_INCOMPLETE,
    )

    unknown_flag = _manifest()
    unknown_flag["tools_are_free"] = True
    _assert_refused(
        _validate(evaluator_manifest=unknown_flag),
        CaseBudgetAdmissionReason.MANIFEST_UNKNOWN,
    )

    extra_evaluator = _manifest()
    extra_evaluator["evaluator_versions"]["unregistered-evaluator"] = "v1"
    _assert_refused(
        _validate(evaluator_manifest=extra_evaluator),
        CaseBudgetAdmissionReason.MANIFEST_UNKNOWN,
    )

    wrong_version = _manifest()
    wrong_version["evaluator_versions"]["trajectory-evaluator"] = "trajectory-v2"
    _assert_refused(
        _validate(evaluator_manifest=wrong_version),
        CaseBudgetAdmissionReason.MANIFEST_VERSION_UNSUPPORTED,
    )


def test_judge_profile_and_ensemble_keys_are_rejected() -> None:
    with_profile = _manifest()
    with_profile["judge_profile"] = {"untrusted": True}
    _assert_refused(
        _validate(evaluator_manifest=with_profile),
        CaseBudgetAdmissionReason.JUDGE_ENSEMBLE_UNSUPPORTED,
    )

    with_ensemble = _manifest()
    with_ensemble["judge_ensemble"] = {"members": []}
    _assert_refused(
        _validate(evaluator_manifest=with_ensemble),
        CaseBudgetAdmissionReason.JUDGE_ENSEMBLE_UNSUPPORTED,
    )

    with_judge_version = _manifest()
    with_judge_version["evaluator_versions"]["answer-quality-judge"] = "jv1-unknown"
    _assert_refused(
        _validate(evaluator_manifest=with_judge_version),
        CaseBudgetAdmissionReason.JUDGE_ENSEMBLE_UNSUPPORTED,
    )


def test_unknown_or_mismatched_frozen_spec_is_not_trusted() -> None:
    unknown = _raw_spec()
    unknown["trusted_for_budget"] = True
    _assert_refused(
        _validate(resolved_spec=unknown),
        CaseBudgetAdmissionReason.FROZEN_SPEC_UNKNOWN,
    )

    raw = _raw_spec()
    mismatched = replace(
        _frozen(raw),
        runtime={"trusted_for_budget": True},
    )
    _assert_refused(
        _validate(resolved_spec=raw, frozen_spec=mismatched),
        CaseBudgetAdmissionReason.FROZEN_SPEC_MISMATCH,
    )


def test_single_primary_and_one_attempt_are_required() -> None:
    fallback = _raw_spec()
    fallback["model"]["fallback_chain"] = [str(PROFILE_ID)]
    _assert_refused(
        _validate(resolved_spec=fallback),
        CaseBudgetAdmissionReason.MODEL_CHAIN_UNSUPPORTED,
    )

    retry = _raw_spec()
    retry["model"]["retry_policy"]["max_attempts"] = 2
    _assert_refused(
        _validate(resolved_spec=retry),
        CaseBudgetAdmissionReason.MODEL_CHAIN_UNSUPPORTED,
    )


def test_unknown_model_windows_and_vision_are_rejected() -> None:
    no_window = _raw_spec()
    no_window["model"]["capabilities"].pop("max_context_tokens")
    _assert_refused(
        _validate(resolved_spec=no_window),
        CaseBudgetAdmissionReason.MODEL_CAPABILITIES_UNKNOWN,
    )

    vision = _raw_spec()
    vision["model"]["capabilities"]["vision"] = True
    _assert_refused(
        _validate(resolved_spec=vision),
        CaseBudgetAdmissionReason.TEXT_GENERATE_UNSUPPORTED,
    )


def test_summary_must_be_disabled_even_when_other_evidence_looks_valid() -> None:
    raw = _raw_spec()
    enabled = replace(
        _frozen(raw),
        runtime={
            **raw["runtime"],
            "summary_policy": {"enabled": True},
        },
    )
    _assert_refused(
        _validate(resolved_spec=raw, frozen_spec=enabled),
        CaseBudgetAdmissionReason.SUMMARY_UNSUPPORTED,
    )


def test_all_tools_are_rejected_including_builtin_read_query_customer() -> None:
    published = _raw_spec()
    published["tools"] = [{"tool_revision_id": str(REVISION_ID)}]
    _assert_refused(
        _validate(resolved_spec=published),
        CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED,
    )

    assert _query_customer_tool().source_kind is ToolSourceKind.BUILTIN
    assert _query_customer_tool().effect is ToolEffect.READ
    _assert_refused(
        _validate(resolved_tools=(_query_customer_tool(),)),
        CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED,
    )

    _assert_refused(
        _validate(resolved_tools=(object(),)),
        CaseBudgetAdmissionReason.TOOL_EVIDENCE_INVALID,
    )


def test_dynamic_history_tool_also_blocks_the_case() -> None:
    raw = _raw_spec()
    raw["runtime"] = {"memory": {"thread_history_search": True, "long_term_memory": False}}
    _assert_refused(
        _validate(resolved_spec=raw),
        CaseBudgetAdmissionReason.TOOLS_UNSUPPORTED,
    )


def test_remote_onyx_retrieval_and_approval_are_rejected() -> None:
    remote = _raw_spec()
    remote["retrieval"]["evidence_set_selector"] = "onyx_v1"
    _assert_refused(
        _validate(resolved_spec=remote),
        CaseBudgetAdmissionReason.RETRIEVAL_UNSUPPORTED,
    )

    approval = _expected()
    approval["approval_decisions"] = ["APPROVED"]
    _assert_refused(
        _validate(expected=approval),
        CaseBudgetAdmissionReason.APPROVAL_UNSUPPORTED,
    )


def test_open_ended_scoring_is_rejected() -> None:
    expected = _expected()
    expected["open_ended"] = True
    _assert_refused(
        _validate(expected=expected),
        CaseBudgetAdmissionReason.OPEN_ENDED_UNSUPPORTED,
    )


def test_paid_memory_writer_and_unknown_request_mode_are_rejected() -> None:
    _assert_refused(
        _validate(memory_writer=object()),
        CaseBudgetAdmissionReason.PAID_MEMORY_WRITER_UNSUPPORTED,
    )
    _assert_refused(
        _validate(request_mode="stream"),
        CaseBudgetAdmissionReason.REQUEST_MODE_UNSUPPORTED,
    )


def test_long_term_memory_selection_without_writer_remains_supported() -> None:
    raw = _raw_spec()
    raw["runtime"] = {
        "memory": {
            "thread_history_search": False,
            "long_term_memory": True,
        }
    }
    result = _validate(resolved_spec=deepcopy(raw), memory_writer=None)

    assert isinstance(result, InteractiveCaseBudgetCandidate)
