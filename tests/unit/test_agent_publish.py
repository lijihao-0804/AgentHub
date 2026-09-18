from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.models import Agent
from packages.agent_runtime.publish import (
    _resolved_spec,
    _validate_retrieval_config,
    _validate_runtime_config,
)
from packages.agent_runtime.tool_revisions import tool_spec_hash, validate_tool_spec
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.snapshots import ResolvedKnowledgeSnapshot
from packages.model_gateway.capabilities import validate_capabilities
from packages.model_gateway.contracts import CapabilityRequirements, ModelCapabilities
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.profile_resolution import ResolvedModelProfile


def _profile(*, profile_id: UUID, workspace_id: UUID) -> ResolvedModelProfile:
    return ResolvedModelProfile(
        id=profile_id,
        workspace_id=workspace_id,
        provider_credential_id=UUID("00000000-0000-0000-0000-000000000001"),
        model="deepseek-chat",
        temperature=Decimal("0.1"),
        max_tokens=2_000,
        timeout_seconds=Decimal("30"),
        fallback_profile_id=None,
        capabilities=ModelCapabilities(
            tool_calling=True,
            streaming=True,
            structured_output=True,
            max_context_tokens=64_000,
        ),
    )


def _agent(**overrides) -> Agent:
    values = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "Support Agent",
        "system_prompt": "Answer from approved enterprise context.",
        "prompt_version": 1,
        "model_profile_id": uuid4(),
        "knowledge_binding_mode": "PINNED",
        "model_retry_policy": {"max_attempts": 2},
        "retrieval_config": {},
        "runtime_config": {},
    }
    values.update(overrides)
    return Agent(**values)


def _snapshot() -> ResolvedKnowledgeSnapshot:
    return ResolvedKnowledgeSnapshot(
        snapshot_id=UUID("00000000-0000-0000-0000-000000000002"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000003"),
        knowledge_base_id=UUID("00000000-0000-0000-0000-000000000004"),
        content_hash="a" * 64,
        snapshot_schema_version=1,
        item_count=2,
        created_at=datetime.now(UTC),
    )


def _spec(agent: Agent) -> dict:
    profile = _profile(profile_id=agent.model_profile_id, workspace_id=agent.workspace_id)
    return _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "deepseek"}),
        snapshots=(_snapshot(),),
        tools=(
            {
                "tool_revision_id": "00000000-0000-0000-0000-000000000005",
                "tool_spec_hash": "b" * 64,
                "effect": "READ",
                "risk_level": "LOW",
                "approval_policy": "NEVER",
            },
        ),
    )


def test_resolved_spec_hash_is_canonical_and_schema_versioned() -> None:
    first = _spec(_agent())
    second = json.loads(json.dumps(first, sort_keys=True))

    assert canonical_json_hash(first) == canonical_json_hash(second)
    assert first["spec_schema_version"] == 1
    assert first["retrieval"]["knowledge_binding_mode"] == "PINNED"
    assert first["retrieval"]["knowledge_snapshot_ids"]


@pytest.mark.parametrize("field", ["system_prompt", "prompt_version", "model_profile_id"])
def test_behavior_changes_change_resolved_spec_hash(field: str) -> None:
    common = {
        "id": UUID("00000000-0000-0000-0000-000000000010"),
        "workspace_id": UUID("00000000-0000-0000-0000-000000000011"),
        "model_profile_id": UUID("00000000-0000-0000-0000-000000000012"),
    }
    first_agent = _agent(**common)
    changed = {
        **common,
        field: (
            "changed prompt"
            if field == "system_prompt"
            else 2
            if field == "prompt_version"
            else uuid4()
        ),
    }
    second_agent = _agent(**changed)

    assert canonical_json_hash(_spec(first_agent)) != canonical_json_hash(_spec(second_agent))


def test_model_projection_and_tool_projection_never_include_secret() -> None:
    resolved = _spec(_agent())
    serialized = json.dumps(resolved, ensure_ascii=False)

    assert "secret" not in serialized.lower()
    assert "credential_ref" in resolved["model"]
    assert "tool_revision_id" in resolved["tools"][0]


def test_tool_spec_hash_is_key_order_independent_and_secret_is_rejected() -> None:
    first = {"effect": "READ", "input_schema": {"type": "object"}}
    second = {"input_schema": {"type": "object"}, "effect": "READ"}

    assert tool_spec_hash(first) == tool_spec_hash(second)
    with pytest.raises(AgentHubError) as raised:
        validate_tool_spec({"effect": "READ", "authorization": "Bearer secret"})
    assert raised.value.code == "TOOL_SPEC_SECRET_FORBIDDEN"


def test_publish_config_validation_rejects_invalid_budget_and_retrieval_limits() -> None:
    with pytest.raises(AgentHubError):
        _validate_runtime_config({"context_budget": {"max_retrieval_tokens": 0}})
    with pytest.raises(AgentHubError):
        _validate_retrieval_config({"candidate_top_k": 2, "final_top_k": 3})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_steps", 33),
        ("max_tool_calls", 65),
        ("max_identical_calls", 5),
        ("max_parallel_reads", 9),
    ],
)
def test_publish_runtime_config_rejects_server_limit_overrides(key: str, value: int) -> None:
    with pytest.raises(AgentHubError):
        _validate_runtime_config({key: value})


def test_tool_calling_capability_mismatch_is_terminal_for_publish() -> None:
    with pytest.raises(ModelGatewayError) as raised:
        validate_capabilities(
            {"tool_calling": False},
            CapabilityRequirements(required=frozenset({"tool_calling"})),
        )
    assert raised.value.code == ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH
