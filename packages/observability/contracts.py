from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class TraceSpan(Protocol):
    async def end(
        self,
        *,
        attributes: Mapping[str, Any] | None = None,
        status: str = "ok",
        failure_code: str | None = None,
    ) -> None: ...


class TraceSink(Protocol):
    async def start_span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceSpan: ...


__all__ = ["TraceSink", "TraceSpan"]
