from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
    Utf8ByteTokenEstimator,
)
from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelToolCall,
    ModelToolDefinition,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


@dataclass(frozen=True)
class _CharEstimator:
    def estimate(self, text: str) -> int:
        return len(text)


def _profile(*, context: int, max_tokens: int) -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="test",
        model="test-model",
        temperature=Decimal("0"),
        max_tokens=max_tokens,
        timeout_seconds=Decimal("30"),
        capabilities=ModelCapabilities(max_context_tokens=context),
    )


def _policy(
    *,
    context: int = 256,
    max_tokens: int = 16,
    fallback_context: int | None = None,
    fallback_max_tokens: int = 16,
    config: ContextBudgetConfig | None = None,
    estimator: _CharEstimator | None = None,
) -> ContextBudgetPolicy:
    primary = _profile(context=context, max_tokens=max_tokens)
    fallbacks = ()
    if fallback_context is not None:
        fallbacks = (_profile(context=fallback_context, max_tokens=fallback_max_tokens),)
    spec = FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(
            primary=primary,
            fallbacks=fallbacks,
            retry_policy=RetryPolicy(),
        ),
        system_prompt="unused by the policy; the caller supplies explicit messages",
        prompt_version=1,
        runtime={},
    )
    return ContextBudgetPolicy(spec, config, estimator=estimator or _CharEstimator())


def test_utf8_estimator_is_deterministic_and_offline() -> None:
    estimator = Utf8ByteTokenEstimator()

    assert estimator.estimate("hello") == 2
    assert estimator.estimate("你好") == 2
    assert estimator.estimate("") == 0


def test_effective_limit_uses_every_frozen_profile_and_reserves_max_output() -> None:
    policy = _policy(
        context=100,
        max_tokens=20,
        fallback_context=80,
        fallback_max_tokens=30,
        config=ContextBudgetConfig(reserved_output_tokens=5),
    )

    assert policy.effective_context_limit == 80
    assert policy.effective_reserved_output_tokens == 30


def test_missing_context_limit_and_reserved_overflow_are_terminal_errors() -> None:
    profile = _profile(context=100, max_tokens=100)
    missing = ResolvedModelExecutionProfile(
        id=profile.id,
        workspace_id=profile.workspace_id,
        provider_credential_id=profile.provider_credential_id,
        provider=profile.provider,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
        capabilities=ModelCapabilities(max_context_tokens=None),
    )
    plan = ResolvedModelExecutionPlan(primary=missing)
    with pytest.raises(AgentHubError) as unavailable:
        _ = ContextBudgetPolicy(plan).context_limit
    assert unavailable.value.code == "AGENT_CONTEXT_LIMIT_UNAVAILABLE"

    with pytest.raises(AgentHubError) as exceeded:
        _ = _policy(context=100, max_tokens=100).reserved_output_tokens
    assert exceeded.value.code == "AGENT_CONTEXT_BUDGET_EXCEEDED"


def test_categories_are_explicit_and_raw_messages_cannot_be_inferred() -> None:
    policy = _policy()
    with pytest.raises(ValueError, match="categories are required"):
        policy.admit((ModelMessage(role="user", content="hello"),))


def test_mandatory_overflow_reports_tool_name_description_and_schema_counts() -> None:
    policy = _policy(
        context=40,
        max_tokens=1,
        config=ContextBudgetConfig(reserved_output_tokens=1),
    )
    tool = ModelToolDefinition(
        name="lookup_customer",
        description="look up a customer",
        parameters={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    messages = (
        ModelMessage(role="system", content="runtime"),
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content="task"),
    )

    with pytest.raises(AgentHubError) as raised:
        policy.admit(
            messages,
            [
                ContextCategory.RUNTIME_POLICY,
                ContextCategory.SYSTEM_PROMPT,
                ContextCategory.CURRENT_USER_TASK,
            ],
            tool_definitions=(tool,),
        )
    assert raised.value.code == "AGENT_CONTEXT_BUDGET_EXCEEDED"
    assert "lookup_customer" in raised.value.message
    assert "description_tokens=" in raised.value.message
    assert "json_schema_tokens=" in raised.value.message


def test_rag_and_tool_results_are_projected_as_legal_bounded_json() -> None:
    policy = _policy(
        context=256,
        max_tokens=1,
        config=ContextBudgetConfig(
            reserved_output_tokens=1,
            max_retrieval_tokens=50,
            max_tool_result_tokens=50,
        ),
    )
    rag = ModelMessage(
        role="system",
        content=json.dumps({"source": "doc-1", "text": "x" * 200}),
    )
    result = ModelMessage(
        role="tool",
        tool_call_id="call-1",
        content=json.dumps(
            {"status": "SUCCESS", "trust": "TRUSTED", "data": {"value": "y" * 200}}
        ),
    )
    assistant = ModelMessage(
        role="assistant",
        content=None,
        tool_calls=(ModelToolCall(name="lookup", arguments={}),),
    )
    admitted = policy.admit(
        (
            ModelMessage(role="user", content="task"),
            rag,
            assistant,
            result,
        ),
        [
            ContextCategory.CURRENT_USER_TASK,
            ContextCategory.RAG_EVIDENCE,
            ContextCategory.CONVERSATION,
            ContextCategory.TOOL_RESULT,
        ],
        [None, None, "latest", "latest"],
    )

    projected_rag = admitted.messages[1].content
    projected_result = admitted.messages[3].content
    assert projected_rag is not None
    assert projected_result is not None
    assert json.loads(projected_rag)["truncated"] is True
    assert json.loads(projected_result)["trust"] == "UNTRUSTED"
    assert json.loads(projected_result)["truncated"] is True
    assert admitted.usage.truncated is True
    assert admitted.usage.estimated_input_after <= admitted.usage.available_input


def test_conversation_drops_oldest_groups_and_keeps_newest() -> None:
    policy = _policy(
        context=20,
        max_tokens=1,
        config=ContextBudgetConfig(
            reserved_output_tokens=1,
            max_retrieval_tokens=0,
            max_tool_result_tokens=0,
        ),
    )
    messages = (
        ModelMessage(role="system", content="p"),
        ModelMessage(role="user", content="t"),
        ModelMessage(role="assistant", content="old-" * 5),
        ModelMessage(role="user", content="new"),
    )
    result = policy.admit(
        messages,
        [
            ContextCategory.RUNTIME_POLICY,
            ContextCategory.CURRENT_USER_TASK,
            ContextCategory.CONVERSATION,
            ContextCategory.CONVERSATION,
        ],
        [None, None, "old", "new"],
    )

    assert [message.content for message in result.messages] == ["p", "t", "new"]
    assert result.usage.dropped_exchange_count == 1


def test_tool_call_and_observation_are_atomic_and_never_orphaned() -> None:
    policy = _policy(
        context=80,
        max_tokens=1,
        config=ContextBudgetConfig(
            reserved_output_tokens=1,
            max_retrieval_tokens=0,
            max_tool_result_tokens=0,
        ),
    )
    old_assistant = ModelMessage(
        role="assistant",
        content=None,
        tool_calls=(ModelToolCall(name="lookup", arguments={}),),
    )
    old_observation = ModelMessage(
        role="tool",
        tool_call_id="old-call",
        content=json.dumps({"trust": "UNTRUSTED", "data": "old"}),
    )
    newest = ModelMessage(role="assistant", content="newest")

    result = policy.admit(
        (
            ModelMessage(role="system", content="p"),
            ModelMessage(role="user", content="t"),
            old_assistant,
            old_observation,
            newest,
        ),
        [
            ContextCategory.RUNTIME_POLICY,
            ContextCategory.CURRENT_USER_TASK,
            ContextCategory.CONVERSATION,
            ContextCategory.TOOL_RESULT,
            ContextCategory.CONVERSATION,
        ],
        [None, None, "old", "old", "new"],
    )

    assert old_assistant not in result.messages
    assert old_observation not in result.messages
    assert newest in result.messages


def test_inline_context_messages_are_supported_without_parallel_metadata() -> None:
    policy = _policy(config=ContextBudgetConfig(reserved_output_tokens=1))
    result = policy.admit(
        (
            ContextMessage(
                ModelMessage(role="system", content="policy"),
                ContextCategory.RUNTIME_POLICY,
            ),
            ContextMessage(
                ModelMessage(role="user", content="task"),
                ContextCategory.CURRENT_USER_TASK,
            ),
        )
    )

    assert result.messages[0].content == "policy"
    assert result.messages[1].content == "task"
    assert result.usage.estimated_input_after == 10


def test_usage_contains_numeric_category_counts_but_no_business_text() -> None:
    policy = _policy(config=ContextBudgetConfig(reserved_output_tokens=1))
    result = policy.admit(
        (
            ContextMessage(
                ModelMessage(role="system", content="secret business text"),
                ContextCategory.RUNTIME_POLICY,
            ),
        )
    )

    usage_text = repr(result.usage)
    assert "secret business text" not in usage_text
    assert result.usage.retained_by_category[ContextCategory.RUNTIME_POLICY] > 0
