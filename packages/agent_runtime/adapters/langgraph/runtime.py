"""Small LangGraph adapter boundary for the provider-neutral runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


def resume_command(payload: Mapping[str, Any]) -> Any:
    return Command(resume=dict(payload))


def approval_interrupt(payload: Mapping[str, Any]) -> Any:
    return interrupt(dict(payload))


def interrupt_payloads(state: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    interrupts = state.get("__interrupt__")
    if not interrupts:
        return ()
    if not isinstance(interrupts, (list, tuple)):
        interrupts = (interrupts,)
    payloads: list[dict[str, Any]] = []
    for item in interrupts:
        value = getattr(item, "value", item)
        if isinstance(value, Mapping):
            payloads.append(dict(value))
    return tuple(payloads)


def compile_agent_graph(
    state_type: type,
    *,
    nodes: Mapping[str, Callable[..., Any]],
    edges: Sequence[tuple[str, str]],
    conditional_edges: Mapping[str, Callable[..., Any]],
    checkpointer: Any | None = None,
) -> Any:
    graph = StateGraph(state_type)
    for name, handler in nodes.items():
        graph.add_node(name, handler)
    for source, target in edges:
        graph.add_edge(
            START if source == "__START__" else source,
            END if target == "__END__" else target,
        )
    for source, router in conditional_edges.items():
        graph.add_conditional_edges(source, router)
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "approval_interrupt",
    "compile_agent_graph",
    "interrupt_payloads",
    "resume_command",
]
