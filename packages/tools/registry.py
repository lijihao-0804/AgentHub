"""Static builtin identity registry; no import, eval, shell or remote dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.knowledge.contracts import KnowledgeRetriever
from packages.tools.builtins import calculate, query_customer, search_knowledge
from packages.tools.contracts import ToolDefinition, ToolExecutionContext

ToolHandler = Callable[
    [ToolExecutionContext, ToolDefinition, Mapping[str, Any], AsyncSession | None],
    Awaitable[dict[str, Any]],
]


class ToolRegistry:
    """The production registry contains only the three M4-B builtin identities."""

    def __init__(
        self,
        *,
        retriever: KnowledgeRetriever | None = None,
        handler_overrides: Mapping[str, ToolHandler] | None = None,
    ) -> None:
        handlers: dict[str, ToolHandler] = {
            "calculator": calculate,
            "query_customer": query_customer,
            "search_knowledge": partial(search_knowledge, retriever=retriever),
        }
        if handler_overrides:
            # This hook is for deterministic tests only; production composition does not
            # receive arbitrary identities and still resolves only known builtin names.
            handlers.update(
                {
                    identity: handler
                    for identity, handler in handler_overrides.items()
                    if identity in handlers
                }
            )
        self._handlers = handlers

    def resolve(self, identity: str) -> ToolHandler | None:
        return self._handlers.get(identity)


__all__ = ["ToolHandler", "ToolRegistry"]
