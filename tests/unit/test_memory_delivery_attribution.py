"""Final input, not a selected ID or a partial fact, controls Memory touch."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.runtime_graph import (
    _AgentRunGraph,
    _memory_delivery_metadata,
    _retain_admission,
)
from packages.agent_runtime.runtime_projection import _memory_message, _safe_step_metadata
from packages.memory.contracts import SelectedMemory
from packages.model_gateway.contracts import ModelMessage, ModelRequest
from tests.unit.test_m4d_context_budget import _policy
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec, _model_graph


def _memories():
    return (
        SelectedMemory(uuid4(), "CONSTRAINT", "背景" * 150 + "，但不得绕过审批。", 1),
        SelectedMemory(uuid4(), "CONSTRAINT", "退款以人民币结算，超过30天不得自动退款。", 1),
    )


def _admit(memories, *, memory_budget=200, context=2_000):
    messages = (
        ContextMessage(ModelMessage("system", "policy"), ContextCategory.RUNTIME_POLICY),
        ContextMessage(_memory_message(memories), ContextCategory.MEMORY),
        ContextMessage(ModelMessage("user", "current task"), ContextCategory.CURRENT_USER_TASK),
    )
    return _policy(
        context=context,
        max_tokens=1,
        config=ContextBudgetConfig(reserved_output_tokens=1, max_memory_tokens=memory_budget),
    ).admit(messages)


def _touch_graph():
    touched = []

    async def touch(*, workspace_id, memory_ids):
        touched.append((workspace_id, memory_ids))

    graph = _AgentRunGraph.__new__(_AgentRunGraph)
    graph.context = SimpleNamespace(workspace_id=str(uuid4()))
    graph.service = SimpleNamespace(memory_selector=SimpleNamespace(touch=touch))
    return graph, touched


@pytest.mark.asyncio
async def test_only_the_whole_final_fact_is_touched_in_the_original_workspace():
    memories = _memories()
    admission = _admit(memories)
    state = {"memory_ids": tuple(item.id for item in memories)}
    graph, touched = _touch_graph()

    await graph._touch_admitted_memories(state, admission)

    payload = next(
        json.loads(message.content)
        for message, category in zip(admission.messages, admission.message_categories, strict=True)
        if category is ContextCategory.MEMORY
    )
    assert payload["memories"] == [
        {"id": str(memories[1].id), "kind": "CONSTRAINT", "content": memories[1].content}
    ]
    assert touched == [(UUID(graph.context.workspace_id), (memories[1].id,))]


def test_drops_remain_attributed_after_a_later_round_has_no_memory_message():
    memories = _memories()
    state = {"memory_ids": tuple(item.id for item in memories)}
    first = _admit(memories)
    metadata = _memory_delivery_metadata(state, first)
    assert metadata["memory_selected_count"] == 2
    assert metadata["memory_admitted_count"] == 1
    assert metadata["memory_dropped_pool_budget_count"] == 1
    _retain_admission(state, first)
    assert state["memory_ids"] == tuple(item.id for item in memories)

    # A subsequent hard-window decision removes the remaining optional block.
    second = _admit((memories[1],), memory_budget=1_000, context=24)
    second_metadata = _memory_delivery_metadata(state, second)
    _retain_admission(state, second)
    assert state["memory_message_count"] == 0
    assert second_metadata["memory_selected_count"] == 2
    assert second_metadata["memory_dropped_count"] == 2
    assert second_metadata["memory_dropped_pool_budget_count"] == 1
    assert second_metadata["memory_dropped_context_window_count"] == 1

    third = _policy(
        context=24, max_tokens=1, config=ContextBudgetConfig(reserved_output_tokens=1)
    ).admit([ContextMessage(ModelMessage("user", "task"), ContextCategory.CURRENT_USER_TASK)])
    assert _memory_delivery_metadata(state, third) == second_metadata


@pytest.mark.asyncio
async def test_final_window_eviction_does_not_touch_a_pool_admitted_id():
    memory = _memories()[1]
    admission = _admit((memory,), memory_budget=1_000, context=24)
    graph, touched = _touch_graph()
    await graph._touch_admitted_memories({"memory_ids": (memory.id,)}, admission)
    assert touched == []


def test_pool_and_final_window_losses_keep_distinct_causes_in_the_same_call():
    memories = _memories()
    admission = _admit(memories, memory_budget=200, context=24)
    report = admission.memory_admission
    assert report.admitted_ids == ()
    assert report.dropped_reasons == {
        str(memories[0].id): "MEMORY_POOL_BUDGET",
        str(memories[1].id): "CONTEXT_WINDOW_BUDGET",
    }


@pytest.mark.asyncio
async def test_memory_outside_the_frozen_selection_is_not_touched():
    memory = _memories()[1]
    admission = _admit((memory,), memory_budget=1_000)
    graph, touched = _touch_graph()
    await graph._touch_admitted_memories({"memory_ids": (uuid4(),)}, admission)
    assert touched == []


@pytest.mark.asyncio
async def test_system_prompt_with_memory_shaped_json_cannot_trigger_touch():
    memory = _memories()[1]
    admission = _admit((memory,), memory_budget=1_000)
    forged = replace(
        admission,
        message_categories=tuple(ContextCategory.SYSTEM_PROMPT for _ in admission.messages),
    )
    graph, touched = _touch_graph()
    await graph._touch_admitted_memories({"memory_ids": (memory.id,)}, forged)
    assert touched == []


@pytest.mark.asyncio
async def test_id_without_kind_and_body_cannot_be_touched_even_with_a_claimed_report():
    memory = _memories()[1]
    admission = _admit((memory,), memory_budget=1_000)
    messages = tuple(
        replace(
            message,
            content=json.dumps({"trust": "UNTRUSTED", "memories": [{"id": str(memory.id)}]}),
        )
        if category is ContextCategory.MEMORY
        else message
        for message, category in zip(admission.messages, admission.message_categories, strict=True)
    )
    graph, touched = _touch_graph()
    await graph._touch_admitted_memories(
        {"memory_ids": (memory.id,)}, replace(admission, messages=messages)
    )
    assert touched == []


def test_default_trace_projection_exposes_counts_and_reasons_without_business_content():
    memories = _memories()
    state = {"memory_ids": tuple(item.id for item in memories)}
    metadata = _memory_delivery_metadata(state, _admit(memories))
    safe = _safe_step_metadata({**metadata, "memories": [item.content for item in memories]})
    assert safe == metadata
    assert safe["memory_projection_version"] == "memory_atomic_v1"
    assert all(
        str(item.id) not in json.dumps(safe) and item.content not in json.dumps(safe)
        for item in memories
    )


def test_memory_off_keeps_the_default_trace_and_context_unchanged():
    admission = _policy(
        context=100, max_tokens=1, config=ContextBudgetConfig(reserved_output_tokens=1)
    ).admit([ContextMessage(ModelMessage("user", "task"), ContextCategory.CURRENT_USER_TASK)])
    state = {}
    assert _memory_delivery_metadata(state, admission) == {}
    assert state == {}


@pytest.mark.asyncio
async def test_real_model_request_and_model_step_agree_on_admitted_memories():
    requests: list[ModelRequest] = []
    memories = _memories()
    admission = _admit(memories)
    graph = _model_graph(request_sink=requests)
    touches = []

    async def touch(*, workspace_id, memory_ids):
        touches.extend(memory_ids)

    graph.service.memory_selector = SimpleNamespace(touch=touch)
    graph._touch_admitted_memories = _AgentRunGraph._touch_admitted_memories.__get__(graph)
    steps = []

    async def step(kind, status, metadata):
        steps.append((kind, status, _safe_step_metadata(metadata)))

    graph.step = step
    graph._admit_context = lambda _state: admission
    state = {
        "messages": [ModelMessage("user", "question")],
        "runtime": {"max_steps": 3},
        "spec": _frozen_spec(2_000),
        "tool_definitions": {},
        "memory_ids": tuple(item.id for item in memories),
    }
    result = await graph.model(state)
    assert result["final_output"] == "done"
    assert len(requests) == 1
    assert requests[0].messages == admission.messages
    assert touches == [memories[1].id]
    assert steps[-1][2]["memory_selected_count"] == 2
    assert steps[-1][2]["memory_admitted_count"] == 1
    assert str(memories[0].id) in result["memory_drop_reasons"]
