"""M2 final dispatch limits and accounting through real frozen contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.agent_runtime.context_budget import (
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.events import AgentEventEmitter, AgentEventType
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import _resolved_spec
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.context_metadata import ContextMetadata
from packages.model_gateway.contracts import ModelCapabilities, ModelMessage, ModelToolDefinition
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from tests.unit.test_agent_publish import _agent
from tests.unit.test_runtime_rag_evidence_projection import _model_graph


class Chars:
    def estimate(self, text):
        return len(text)


def _profile(*, total=1_000_000, output=2048, metadata=None, workspace=None):
    return ResolvedModelProfile(
        id=uuid4(),
        workspace_id=workspace or uuid4(),
        provider_credential_id=uuid4(),
        model="offline-deployment",
        temperature=Decimal("0"),
        max_tokens=output,
        timeout_seconds=Decimal("30"),
        fallback_profile_id=None,
        capabilities=ModelCapabilities(
            tool_calling=True, streaming=True, max_context_tokens=total, context_metadata=metadata
        ),
    )


def _freeze(profiles, *, adaptive=True):
    agent = _agent(
        workspace_id=profiles[0].workspace_id,
        model_profile_id=profiles[0].id,
        runtime_config={"context_policy": {"policy_version": "adaptive_context_v1"}}
        if adaptive
        else {},
    )
    raw = _resolved_spec(
        agent=agent, model=(profiles, {p.id: "offline" for p in profiles}), bindings=(), tools=()
    )
    return parse_frozen_agent_spec(raw, workspace_id=agent.workspace_id)


def test_64k_final_input_includes_protocol_envelope_at_the_exact_boundary():
    spec = _freeze((_profile(),))
    policy = ContextBudgetPolicy(spec, estimator=Chars())

    def task(text):
        return [ContextMessage(ModelMessage("user", text), ContextCategory.CURRENT_USER_TASK)]

    admitted = policy.admit(task("x" * 65524))
    assert admitted.usage.available_input == 65536
    assert admitted.usage.estimated_input_after == 65536  # 8 request + 4 message, exactly once.
    with pytest.raises(AgentHubError):
        policy.admit(task("x" * 65525))


def test_per_model_output_reserve_is_not_subtracted_again_from_the_small_fallback():
    primary = _profile(output=16384)
    fallback = _profile(total=32768, output=2048, workspace=primary.workspace_id)
    spec = _freeze((primary, fallback))
    result = ContextBudgetPolicy(spec, estimator=Chars()).admit(
        [ContextMessage(ModelMessage("user", "x" * 17000), ContextCategory.CURRENT_USER_TASK)]
    )
    assert result.usage.context_limit == 32768
    assert result.usage.reserved_output == 16384
    assert result.usage.available_input == 29184
    assert result.usage.estimated_input_after == 17012


def test_schema_and_role_protocol_charges_are_once_per_actual_input_item():
    profile = _profile()
    adaptive = _freeze((profile,))
    legacy = _freeze((profile,), adaptive=False)
    messages = [
        ContextMessage(ModelMessage("system", "policy"), ContextCategory.RUNTIME_POLICY),
        ContextMessage(ModelMessage("user", "task"), ContextCategory.CURRENT_USER_TASK),
    ]
    tools = [
        ModelToolDefinition(
            name="calculator", description="Calculate", parameters={"type": "object"}
        )
    ]
    old = ContextBudgetPolicy(legacy, estimator=Chars()).admit(messages, tool_definitions=tools)
    new = ContextBudgetPolicy(adaptive, estimator=Chars()).admit(messages, tool_definitions=tools)
    assert new.usage.estimated_input_after - old.usage.estimated_input_after == 24

    class AlreadyAccounted(Chars):
        includes_protocol_overhead = True

    included = ContextBudgetPolicy(adaptive, estimator=AlreadyAccounted()).admit(
        messages, tool_definitions=tools
    )
    # An injected estimator cannot override the counter contract frozen at
    # publication; this version declares that protocol overhead is excluded.
    assert included.usage.estimated_input_after == new.usage.estimated_input_after


@pytest.mark.asyncio
async def test_independent_input_without_total_window_reaches_the_fake_gateway_and_valid_event():
    profile = _profile(
        total=None,
        metadata=ContextMetadata(
            deployment_id="isolated-fixture",
            resolved_model="exact-fixture",
            source_version="1",
            max_input_tokens=32768,
            max_output_tokens=4096,
            deployment_input_limit=30000,
            input_output_semantics="INDEPENDENT_INPUT_OUTPUT",
            output_reserve_semantics="INCLUDES_REASONING",
        ),
    )
    spec = _freeze((profile,))
    requests, events = [], []
    graph = _model_graph(request_sink=requests)
    graph.context.workspace_id = str(profile.workspace_id)
    emitter = AgentEventEmitter(
        run_id=str(graph.run.id),
        agent_version_id=str(graph.run.agent_version_id),
        request_id="offline-m2",
    )

    async def emit(kind, data):
        events.append(await emitter.emit(kind, data))

    graph._emit = emit
    steps = []

    async def step(kind, status, metadata):
        steps.append((kind, status, metadata))

    graph.step = step
    state = {
        "spec": spec,
        "runtime": spec.runtime,
        "tool_definitions": {},
        "messages": [
            ModelMessage("system", "policy"),
            ModelMessage("system", "instructions"),
            ModelMessage("user", "current task"),
        ],
    }
    result = await graph.model(state)
    assert result["final_output"] == "done" and len(requests) == 1
    budget = next(event for event in events if event.type is AgentEventType.CONTEXT_BUDGET)
    assert "context_limit" not in budget.data
    assert budget.data["context_limit_status"] == "UNKNOWN"
    assert budget.data["available_input"] == 28500
    assert budget.data["counter_precision"] == "APPROXIMATE"
    assert steps[-1][2]["available_input"] == 28500


def test_mutating_a_working_limit_cannot_bypass_frozen_proof_at_dispatch():
    original = _freeze((_profile(),))
    runtime = deepcopy(original.runtime)
    runtime["context_policy"]["input_limit_tokens"] += 1
    forged = replace(original, runtime=runtime)
    with pytest.raises(AgentHubError) as error:
        ContextBudgetPolicy(forged)
    assert error.value.code == "AGENT_CONTEXT_POLICY_INVALID"


def test_adaptive_budget_rejects_an_estimator_that_ignores_nonempty_input():
    class ZeroEstimator:
        def estimate(self, text):
            return 0

    policy = ContextBudgetPolicy(_freeze((_profile(),)), estimator=ZeroEstimator())
    with pytest.raises(ValueError, match="non-empty text"):
        policy.admit(
            [ContextMessage(ModelMessage("user", "important"), ContextCategory.CURRENT_USER_TASK)]
        )
