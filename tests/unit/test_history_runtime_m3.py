"""The runtime bridge counts uncropped source messages with the M2 estimator."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.context_budget import ContextBudgetPolicy
from packages.agent_runtime.history_planner import PlanPressure
from packages.agent_runtime.history_runtime import _namespace_tool_ids, plan_history_view
from packages.agent_runtime.work_layer import ThreadConversation, ThreadTurnContext
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from tests.unit.test_context_policy_m2_runtime import Chars, _freeze, _profile


def _conversation(count: int):
    workspace_id, thread_id = uuid4(), uuid4()
    sources, turns = [], []
    for index in range(count):
        turn_id, run_id = uuid4(), uuid4()
        sources.append(
            SimpleNamespace(
                source_id=f"thread-turn:{turn_id}",
                turn_id=turn_id,
                run_id=run_id,
                sequence=index + 1,
                revision_sha256=f"revision-{index}",
            )
        )
        turns.append(
            ThreadTurnContext(
                "q" * 950, "a" * 950, turn_id=turn_id, run_id=run_id, sequence=index + 1
            )
        )
    manifest = SimpleNamespace(
        status="COMPLETE",
        selected_source_count=count,
        available_eligible_source_count=count,
        selected_sources=sources,
        scope=SimpleNamespace(
            workspace_id=workspace_id, thread_id=thread_id, before_sequence=count + 1
        ),
    )
    return ThreadConversation(turns=tuple(turns), turns_available=count, source_manifest=manifest)


def test_runtime_counts_all_history_before_legacy_ten_turn_cut():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    result = plan_history_view(
        policy=policy,
        conversation=_conversation(12),
        mandatory_messages=(ModelMessage("user", "question"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
    )
    assert result.plan.pre_trim_pressure is PlanPressure.SOFT
    assert result.plan.pre_trim_tokens > result.plan.thresholds.soft_tokens
    assert len(result.messages) == 24


def test_runtime_refuses_a_partially_measured_history():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    conversation = _conversation(2)
    conversation.source_manifest.available_eligible_source_count = 3
    with pytest.raises(AgentHubError) as error:
        plan_history_view(
            policy=policy,
            conversation=conversation,
            mandatory_messages=(ModelMessage("user", "question"),),
            memory_messages=(),
            tools=(),
            permission_snapshot="user-with-run-permission",
        )
    assert error.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"


def test_blocked_plan_can_be_examined_without_creating_a_sendable_view():
    policy = ContextBudgetPolicy(_freeze((_profile(total=32768),)), estimator=Chars())
    arguments = dict(
        policy=policy,
        conversation=_conversation(18),
        mandatory_messages=(ModelMessage("user", "question"),),
        memory_messages=(),
        tools=(),
        permission_snapshot="user-with-run-permission",
    )
    with pytest.raises(AgentHubError) as error:
        plan_history_view(**arguments)
    assert error.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    blocked = plan_history_view(**arguments, return_blocked_plan=True)
    assert not blocked.plan.ready_for_dispatch
    assert blocked.plan.pre_trim_pressure is PlanPressure.HARD
    assert blocked.messages == blocked.memory_messages == blocked.exchange_ids == ()


def test_protocol_ids_are_isolated_between_different_historical_turns():
    messages = (
        ModelMessage("user", "question"),
        ModelMessage("assistant", tool_calls=(ModelToolCall("lookup", {}, "call-1"),)),
        ModelMessage("tool", "result", name="lookup", tool_call_id="call-1"),
        ModelMessage("assistant", "answer"),
    )
    first = _namespace_tool_ids(uuid4().hex, messages)
    second = _namespace_tool_ids(uuid4().hex, messages)
    first_id, second_id = (
        first[1].tool_calls[0].provider_tool_call_id,
        second[1].tool_calls[0].provider_tool_call_id,
    )
    assert first_id != second_id
    assert first[2].tool_call_id == first_id and second[2].tool_call_id == second_id
