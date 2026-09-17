from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str
    usage: ModelUsage | None
    finish_reason: str | None


class ModelGateway(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]: ...
