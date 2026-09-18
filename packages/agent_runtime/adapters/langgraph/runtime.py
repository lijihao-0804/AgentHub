"""Small LangGraph adapter boundary for the provider-neutral runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langgraph.graph import END, START, StateGraph


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


__all__ = ["compile_agent_graph"]
