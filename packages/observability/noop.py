"""No-op observability implementation used when no backend is configured."""

from collections.abc import Mapping
from typing import Any


class NoopTraceSpan:
    async def end(
        self,
        *,
        attributes: Mapping[str, Any] | None = None,
        status: str = "ok",
        failure_code: str | None = None,
    ) -> None:
        del attributes, status, failure_code


class NoopTraceSink:
    async def start_span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> NoopTraceSpan:
        del name, attributes
        return NoopTraceSpan()


__all__ = ["NoopTraceSink", "NoopTraceSpan"]
