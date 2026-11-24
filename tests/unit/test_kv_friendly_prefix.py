"""Unit tests for KV-cache-friendly static prefix ordering and tail turn reminders."""

from __future__ import annotations

from uuid import UUID

from packages.agent_runtime.analysis_table import analysis_turn_instruction
from packages.agent_runtime.kv_prefix import append_turn_reminders, static_prefix_hash
from packages.agent_runtime.memory_tools import history_hint
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.model_gateway.contracts import ModelMessage
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
    ToolSourceKind,
)


def _tool(identity: str, description: str = "desc") -> ToolDefinition:
    return ToolDefinition(
        identity=identity,
        revision_id=UUID(int=1),
        spec_hash="hash",
        description=description,
        input_schema={"type": "object", "properties": {}},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=10,
        source_kind=ToolSourceKind.BUILTIN,
    )


def test_model_tool_definitions_are_deterministically_sorted_for_kv_cache() -> None:
    tools_a = {"z_tool": _tool("z_tool"), "a_tool": _tool("a_tool"), "m_tool": _tool("m_tool")}
    tools_b = {"m_tool": _tool("m_tool"), "z_tool": _tool("z_tool"), "a_tool": _tool("a_tool")}

    defs_a = _AgentRunGraph._model_tool_definitions(tools_a)
    defs_b = _AgentRunGraph._model_tool_definitions(tools_b)

    assert [t.name for t in defs_a] == ["a_tool", "m_tool", "z_tool"]
    assert defs_a == defs_b
    sys_msgs = (ModelMessage("system", "policy"), ModelMessage("system", "prompt"))
    assert static_prefix_hash(defs_a, sys_msgs) == static_prefix_hash(defs_b, sys_msgs)


def test_append_turn_reminders_keeps_normal_user_text_unchanged() -> None:
    question = "What is the Q3 revenue?"
    assert append_turn_reminders(question, ()) == question
    assert append_turn_reminders(question, ("", "   ")) == question


def test_dynamic_reminders_stay_in_tail_without_mutating_static_prefix() -> None:
    tools = _AgentRunGraph._model_tool_definitions({"search_knowledge": _tool("search_knowledge")})
    static_system = (
        ModelMessage("system", "policy\n\n" + history_hint(turns_dropped_by_window=0)),
        ModelMessage("system", "You are a data analyst."),
    )
    turn1_prefix = static_prefix_hash(tools, static_system)
    turn2_prefix = static_prefix_hash(tools, static_system)
    assert turn1_prefix == turn2_prefix

    # Turn 1: no dropped turns, normal prose question
    t1_user = append_turn_reminders("Explain the trend.", ())
    assert t1_user == "Explain the trend."

    # Turn 2: 5 dropped turns + table-only question -> dynamic rules go to tail only
    rule = analysis_turn_instruction(
        {"analysis_answer_policy": "grounded_table_v2"},
        "Return a table only of paying users.",
    )
    assert rule is not None
    t2_user = append_turn_reminders(
        "Return a table only of paying users.",
        (history_hint(turns_dropped_by_window=5), rule),
    )
    assert "<turn_context>" in t2_user
    assert "5 earlier turn(s)" in t2_user
    assert rule in t2_user
    assert "5 earlier turn(s)" not in static_system[0].content
    assert rule not in static_system[1].content
