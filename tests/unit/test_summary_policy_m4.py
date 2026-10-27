"""M4 summary activation is explicit, versioned, and opt-in."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.schemas.agents import RuntimeConfigRequest
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import (
    SUMMARY_SPEC_SCHEMA_VERSION,
    AgentPublishService,
    _draft_runtime_from_version,
    _resolved_spec,
    _validate_runtime_config,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from tests.unit.test_agent_publish import _agent
from tests.unit.test_context_policy_m2_runtime import _profile


def _runtime_with_summary() -> dict:
    return {
        "context_policy": {"policy_version": "adaptive_context_v1"},
        "history_policy": "deterministic_history_v1",
        "summary_policy": {
            "enabled": True,
            "policy_version": "thread_summary_policy_v1",
            "prompt_version": "summary_candidate_zh_v1",
            "validator_version": "summary_mechanical_v1",
            "tokenizer_id": "approx_language_v1",
            "tokenizer_version": 1,
            "soft_trigger_percent": 70,
            "hard_trigger_percent": 85,
            "target_percent": 55,
        },
    }


def _live_spec():
    profile = _profile()
    agent = _agent(
        workspace_id=profile.workspace_id,
        model_profile_id=profile.id,
        runtime_config=_runtime_with_summary(),
    )
    raw = _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "offline"}),
        bindings=(),
        tools=(),
    )
    return agent, raw


@pytest.mark.parametrize(
    ("runtime_config", "expected_schema"),
    [
        ({}, 2),
        ({"context_policy": {"policy_version": "adaptive_context_v1"}}, 3),
        (
            {
                "context_policy": {"policy_version": "adaptive_context_v1"},
                "history_policy": "deterministic_history_v1",
            },
            4,
        ),
    ],
)
def test_summary_is_absent_by_default_and_keeps_existing_schema_selection(
    runtime_config: dict,
    expected_schema: int,
) -> None:
    profile = _profile()
    agent = _agent(
        workspace_id=profile.workspace_id,
        model_profile_id=profile.id,
        runtime_config=runtime_config,
    )
    raw = _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "offline"}),
        bindings=(),
        tools=(),
    )

    assert raw["spec_schema_version"] == expected_schema
    assert "summary_policy" not in raw["runtime"]


def test_enabled_summary_is_frozen_in_schema_five_with_adaptive_history_prerequisites():
    agent, raw = _live_spec()

    assert raw["spec_schema_version"] == SUMMARY_SPEC_SCHEMA_VERSION == 5
    assert raw["runtime"]["summary_policy"] == _runtime_with_summary()["summary_policy"]
    frozen = parse_frozen_agent_spec(raw, workspace_id=agent.workspace_id)
    assert frozen.runtime["summary_policy"] == raw["runtime"]["summary_policy"]
    assert frozen.runtime["history_policy"] == "deterministic_history_v1"
    assert frozen.runtime["context_policy"]["policy_version"] == "adaptive_context_v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preflight", "publish"])
async def test_failed_summary_quality_gate_blocks_preflight_and_publication(operation):
    profile = _profile()
    agent = _agent(
        workspace_id=profile.workspace_id,
        model_profile_id=profile.id,
        runtime_config=_runtime_with_summary(),
    )
    context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="test", trace_id="test", user_id=str(uuid4())),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(profile.workspace_id),
        permissions=frozenset({"agent_edit"}),
    )
    service = AgentPublishService()
    service._load_agent = AsyncMock(return_value=agent)
    service._resolve_model = AsyncMock(return_value=((profile,), {profile.id: "offline"}))
    service._resolve_knowledge = AsyncMock()
    session = AsyncMock()

    with pytest.raises(AgentHubError) as blocked:
        await getattr(service, operation)(session, context, agent.id)
    assert blocked.value.code == "SUMMARY_QUALITY_GATE_NOT_MET"
    assert blocked.value.status_code == 422
    service._resolve_knowledge.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("missing", ["context_policy", "history_policy"])
def test_summary_requires_adaptive_and_deterministic_history_prerequisites(
    missing: str,
) -> None:
    runtime = _runtime_with_summary()
    del runtime[missing]

    with pytest.raises(AgentHubError) as raised:
        _validate_runtime_config(runtime)

    assert raised.value.code == "INVALID_SUMMARY_POLICY"


@pytest.mark.parametrize(
    "summary_policy",
    [
        {},
        {"policy_version": "thread_summary_policy_v1"},
        {"enabled": False},
        {"enabled": 1},
        {"enabled": True, "policy_version": "thread_summary_policy_v2"},
        {"enabled": True, "prompt_version": "summary_candidate_zh_v2"},
        {"enabled": True, "validator_version": "summary_mechanical_v2"},
        {"enabled": True, "tokenizer_id": "exact_v1"},
        {"enabled": True, "tokenizer_version": "1"},
        {"enabled": True, "soft_trigger_percent": 69},
        {"enabled": True, "hard_trigger_percent": 84},
        {"enabled": True, "target_percent": 54},
        {"enabled": True, "model": "silent-provider-selection"},
    ],
)
def test_publish_rejects_unknown_or_drifting_summary_parameters(
    summary_policy: dict,
) -> None:
    runtime = _runtime_with_summary()
    runtime["summary_policy"] = summary_policy

    with pytest.raises(AgentHubError) as raised:
        _validate_runtime_config(runtime)

    assert raised.value.code == "INVALID_SUMMARY_POLICY"


@pytest.mark.parametrize(
    ("schema_version", "mutation"),
    [
        (4, "summary_policy"),
        (5, "remove_history"),
        (5, "remove_summary"),
        (5, "unknown_summary_field"),
        (5, "integer_enabled"),
        (5, "float_threshold"),
        (5, "wrong_prompt"),
    ],
)
def test_frozen_parser_rejects_inconsistent_schema_five(
    schema_version: int,
    mutation: str,
) -> None:
    agent, raw = _live_spec()
    invalid = deepcopy(raw)
    invalid["spec_schema_version"] = schema_version
    if mutation == "remove_history":
        del invalid["runtime"]["history_policy"]
    elif mutation == "remove_summary":
        del invalid["runtime"]["summary_policy"]
    elif mutation == "unknown_summary_field":
        invalid["runtime"]["summary_policy"]["model_profile_id"] = "default"
    elif mutation == "integer_enabled":
        invalid["runtime"]["summary_policy"]["enabled"] = 1
    elif mutation == "float_threshold":
        invalid["runtime"]["summary_policy"]["soft_trigger_percent"] = 70.0
    elif mutation == "wrong_prompt":
        invalid["runtime"]["summary_policy"]["prompt_version"] = "unregistered"

    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(invalid, workspace_id=agent.workspace_id)


def test_derived_draft_keeps_only_summary_intent_and_republishes():
    _, raw = _live_spec()
    draft = _draft_runtime_from_version(raw["runtime"])

    assert draft["summary_policy"] == raw["runtime"]["summary_policy"]
    assert "model_available_input_tokens" not in draft["context_policy"]
    assert _validate_runtime_config(draft)["summary_policy"] == draft["summary_policy"]


def test_internal_explicit_opt_in_normalizes_the_complete_policy():
    runtime = _runtime_with_summary()
    runtime["summary_policy"] = {"enabled": True}

    normalized = _validate_runtime_config(runtime)

    assert normalized["summary_policy"] == _runtime_with_summary()["summary_policy"]


def test_api_schema_is_strict_and_requires_an_explicit_opt_in():
    valid = RuntimeConfigRequest.model_validate(
        {
            "context_policy": {"policy_version": "adaptive_context_v1"},
            "history_policy": "deterministic_history_v1",
            "summary_policy": _runtime_with_summary()["summary_policy"],
        }
    )
    assert valid.summary_policy is not None
    assert valid.summary_policy.enabled is True

    for invalid in (
        {"summary_policy": {}},
        {"summary_policy": {"enabled": False}},
        {"summary_policy": {"enabled": 1}},
        {"summary_policy": {"enabled": 1.0}},
        {"summary_policy": {"enabled": True, "unknown": 1}},
        {"summary_policy": {"enabled": True, "soft_trigger_percent": 69}},
        {"summary_policy": {"enabled": True, "tokenizer_version": True}},
    ):
        payload = {
            "context_policy": {"policy_version": "adaptive_context_v1"},
            "history_policy": "deterministic_history_v1",
            **invalid,
        }
        with pytest.raises(ValidationError):
            RuntimeConfigRequest.model_validate(payload)

    with pytest.raises(ValidationError):
        RuntimeConfigRequest.model_validate(
            {"summary_policy": _runtime_with_summary()["summary_policy"]}
        )


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_old_schema_versions_still_reject_new_summary_metadata(
    schema_version: int,
) -> None:
    agent, raw = _live_spec()
    invalid = deepcopy(raw)
    invalid["spec_schema_version"] = schema_version
    if schema_version < 3:
        invalid["runtime"].pop("context_policy")
        invalid["runtime"].pop("context_budget_sources", None)
    if schema_version < 4:
        invalid["runtime"].pop("history_policy")

    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(invalid, workspace_id=agent.workspace_id)
