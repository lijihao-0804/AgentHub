from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.schemas.agents import RetrievalConfigRequest
from apps.api.schemas.knowledge import RetrievalPlaygroundRequest
from packages.agent_runtime.publish import _resolved_spec, _validate_retrieval_config
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.tools.builtins.search_knowledge import _snapshot_queries
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRisk,
)


def test_playground_schema_accepts_budget_boundaries_and_rejects_invalid_values() -> None:
    assert (
        RetrievalPlaygroundRequest(
            query="question",
            knowledge_snapshot_id=uuid4(),
            context_budget_bytes=2,
        ).context_budget_bytes
        == 2
    )
    assert (
        RetrievalPlaygroundRequest(
            query="question",
            knowledge_snapshot_id=uuid4(),
            context_budget_bytes=64_000,
        ).context_budget_bytes
        == 64_000
    )

    for value in (True, False, 1, 64_001, 1.5, "8000"):
        with pytest.raises(ValidationError):
            RetrievalPlaygroundRequest(
                query="question",
                knowledge_snapshot_id=uuid4(),
                context_budget_bytes=value,
            )


def test_agent_schema_and_publish_use_the_same_budget_contract() -> None:
    assert RetrievalConfigRequest(context_budget_bytes=8_000).context_budget_bytes == 8_000
    assert (
        _validate_retrieval_config({"context_budget_bytes": 8_000})["context_budget_bytes"]
        == 8_000
    )

    for value in (True, False, 1, 64_001, 1.5, "8000"):
        with pytest.raises(ValidationError):
            RetrievalConfigRequest(context_budget_bytes=value)
        with pytest.raises(AgentHubError):
            _validate_retrieval_config({"context_budget_bytes": value})


def test_legacy_agent_config_does_not_gain_a_none_budget_key() -> None:
    configured = _validate_retrieval_config({"candidate_top_k": 20})

    assert "context_budget_bytes" not in configured


def test_explicit_none_budget_is_omitted_from_validated_config() -> None:
    configured = _validate_retrieval_config({"context_budget_bytes": None})

    assert "context_budget_bytes" not in configured


def test_resolved_spec_freezes_explicit_budget_and_omits_legacy_none() -> None:
    profile_id = uuid4()
    provider_credential_id = uuid4()
    model = SimpleNamespace(
        id=profile_id,
        provider_credential_id=provider_credential_id,
        model="offline",
        temperature=0,
        max_tokens=128,
        timeout_seconds=5,
        capabilities=SimpleNamespace(
            tool_calling=True,
            streaming=False,
            structured_output=False,
            vision=False,
            max_context_tokens=8_192,
        ),
    )
    model_projection = ((model,), {profile_id: "offline"})

    def agent(retrieval_config):
        return SimpleNamespace(
            model_retry_policy={"max_attempts": 1},
            retrieval_config=retrieval_config,
            knowledge_binding_mode="LATEST",
            system_prompt="Answer.",
            prompt_version=1,
            runtime_config={},
        )

    frozen = _resolved_spec(
        agent=agent({"candidate_top_k": 20, "context_budget_bytes": 8_000}),
        model=model_projection,
        bindings=(),
        tools=(),
    )
    legacy = _resolved_spec(
        agent=agent({"candidate_top_k": 20}),
        model=model_projection,
        bindings=(),
        tools=(),
    )

    assert frozen["retrieval"]["context_budget_bytes"] == 8_000
    assert "context_budget_bytes" not in legacy["retrieval"]


@pytest.mark.asyncio
async def test_search_knowledge_forwards_explicit_budget_without_raising_limit_caps() -> None:
    snapshot_id = uuid4()
    query = (
        await _snapshot_queries(
            _SnapshotSession(snapshot_id),
            _tool_context(),
            _definition(
                snapshot_id,
                {
                    "knowledge_binding_mode": "PINNED",
                    "context_budget_bytes": 8_000,
                    "final_top_k": 20,
                },
            ),
            text="question",
            limit=5,
        )
    )[0]

    assert query.context_budget_bytes == 8_000
    assert query.final_top_k == 5


@pytest.mark.asyncio
async def test_search_knowledge_preserves_legacy_defaults() -> None:
    snapshot_id = uuid4()
    query = (
        await _snapshot_queries(
            _SnapshotSession(snapshot_id),
            _tool_context(),
            _definition(snapshot_id, {"knowledge_binding_mode": "PINNED"}),
            text="question",
            limit=5,
        )
    )[0]

    assert query.context_budget_bytes is None
    assert query.final_top_k == 5


class _SnapshotSession:
    def __init__(self, snapshot_id) -> None:
        self.snapshot_id = snapshot_id

    async def scalar(self, _statement):
        return SimpleNamespace(
            id=self.snapshot_id,
            knowledge_base_id=uuid4(),
            content_hash="a" * 64,
        )


def _tool_context() -> ToolExecutionContext:
    workspace_context = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        permissions=frozenset({"knowledge_run"}),
    )
    return ToolExecutionContext(
        workspace_context=workspace_context,
        agent_version_id=uuid4(),
        tool_call_id="tool-call",
    )


def _definition(snapshot_id, retrieval_config: dict[str, object]) -> ToolDefinition:
    return ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="spec-hash",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        snapshot_refs=(
            {
                "snapshot_id": str(snapshot_id),
                "snapshot_hash": "a" * 64,
            },
        ),
        retrieval_config=retrieval_config,
    )
