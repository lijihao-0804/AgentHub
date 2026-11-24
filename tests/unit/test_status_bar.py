"""Unit tests for the code-maintained tail <agent_status> bar."""

from __future__ import annotations

from packages.agent_runtime.status_bar import (
    STATUS_TAG_CLOSE,
    STATUS_TAG_OPEN,
    AgentStatusSnapshot,
    attach_tail_status_bar,
    format_agent_status,
)
from packages.model_gateway.contracts import ModelMessage


def test_format_agent_status_renders_deterministic_counters_and_guards() -> None:
    snap = AgentStatusSnapshot(
        model_round=3,
        max_steps=8,
        tool_call_count=5,
        max_tool_calls=12,
        tool_usage_counts={"search_knowledge": 3, "query_customer": 2},
        tool_failure_counts={"query_customer": 1},
        max_identical_calls=2,
        max_observed_identical_calls=2,
        estimated_input_tokens=6400,
        available_input_tokens=10000,
        pending_approvals=1,
    )
    rendered = format_agent_status(snap)
    assert rendered.startswith(STATUS_TAG_OPEN)
    assert rendered.endswith(STATUS_TAG_CLOSE)
    assert "Steps: 3/8 (remaining: 5)" in rendered
    assert "Tool calls: 5/12 (remaining: 7)" in rendered
    assert "query_customer=2 (1 failed), search_knowledge=3" in rendered
    assert "identical call limit reached (2/2)" in rendered
    assert "6400/10000 tokens (64.0%)" in rendered
    assert "Pending human approvals: 1" in rendered


def test_attach_tail_status_bar_preserves_prefix_and_replaces_tail_in_place() -> None:
    prefix = [
        ModelMessage(role="system", content="static policy"),
        ModelMessage(role="user", content="help me check order 42"),
    ]
    snap1 = AgentStatusSnapshot(model_round=1, max_steps=8, tool_call_count=1, max_tool_calls=12)
    with_bar1 = attach_tail_status_bar(prefix, snap1)
    assert len(with_bar1) == 3
    assert with_bar1[:2] == prefix
    assert "Steps: 1/8" in with_bar1[-1].content

    snap2 = AgentStatusSnapshot(model_round=2, max_steps=8, tool_call_count=2, max_tool_calls=12)
    with_bar2 = attach_tail_status_bar(with_bar1, snap2)
    # Tail status replaced in place without duplicating status blocks
    assert len(with_bar2) == 3
    assert with_bar2[:2] == prefix
    assert "Steps: 2/8" in with_bar2[-1].content
