"""The M3 history strategy is an explicit immutable publication choice."""

from copy import deepcopy

import pytest

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import (
    _draft_runtime_from_version,
    _resolved_spec,
    _validate_runtime_config,
)
from packages.core.errors.exceptions import AgentHubError
from tests.unit.test_agent_publish import _agent
from tests.unit.test_context_policy_m2_runtime import _profile


def _spec(history: bool):
    profile = _profile()
    runtime = {"context_policy": {"policy_version": "adaptive_context_v1"}}
    if history:
        runtime["history_policy"] = "deterministic_history_v1"
    agent = _agent(
        workspace_id=profile.workspace_id,
        model_profile_id=profile.id,
        runtime_config=runtime,
    )
    raw = _resolved_spec(
        agent=agent, model=((profile,), {profile.id: "offline"}), bindings=(), tools=()
    )
    return agent, raw


def test_history_policy_is_versioned_and_does_not_change_existing_m2_behavior():
    agent, raw = _spec(True)
    assert raw["spec_schema_version"] == 4
    frozen = parse_frozen_agent_spec(raw, workspace_id=agent.workspace_id)
    assert frozen.runtime["history_policy"] == "deterministic_history_v1"
    old_agent, old = _spec(False)
    assert old["spec_schema_version"] == 3
    assert (
        "history_policy"
        not in parse_frozen_agent_spec(old, workspace_id=old_agent.workspace_id).runtime
    )


def test_history_policy_requires_adaptive_budget_and_frozen_version():
    with pytest.raises(AgentHubError):
        _validate_runtime_config({"history_policy": "deterministic_history_v1"})
    agent, raw = _spec(True)
    for change in ("missing", "unknown", "old_version"):
        invalid = deepcopy(raw)
        if change == "missing":
            del invalid["runtime"]["history_policy"]
        elif change == "unknown":
            invalid["runtime"]["history_policy"] = "unrecognized"
        else:
            invalid["spec_schema_version"] = 3
        with pytest.raises(AgentHubError):
            parse_frozen_agent_spec(invalid, workspace_id=agent.workspace_id)


def test_version_can_be_cloned_to_a_valid_draft_without_exposing_budget_proof():
    _, raw = _spec(True)
    draft = _draft_runtime_from_version(raw["runtime"])
    assert draft["history_policy"] == "deterministic_history_v1"
    assert "models" not in draft["context_policy"]
    checked = _validate_runtime_config(draft)
    assert checked["history_policy"] == "deterministic_history_v1"
