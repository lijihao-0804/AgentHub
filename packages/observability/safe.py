"""Provider-neutral production trace sink with redacted structured logging."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from packages.observability.contracts import TraceSpan

logger = logging.getLogger("agenthub.observability")
_REDACTED_KEY_PARTS = ("prompt", "content", "credential", "secret", "password", "argument")


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        normalized = str(key).lower()
        if any(part in normalized for part in _REDACTED_KEY_PARTS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[str(key)] = value
        elif isinstance(value, (list, tuple)):
            result[str(key)] = [str(item) for item in value[:32]]
    return result


class SafeTraceSpan:
    def __init__(self, name: str, attributes: Mapping[str, Any] | None) -> None:
        self.name = name
        self.attributes = _safe_attributes(attributes)
        self.started = time.perf_counter()

    async def end(
        self,
        *,
        attributes: Mapping[str, Any] | None = None,
        status: str = "ok",
        failure_code: str | None = None,
    ) -> None:
        payload = {
            **self.attributes,
            **_safe_attributes(attributes),
            "span": self.name,
            "status": status,
            "latency_ms": round((time.perf_counter() - self.started) * 1000, 3),
            "failure_code": failure_code,
        }
        try:
            logger.info("agenthub_trace_span", extra={"trace_span": payload})
        except Exception:
            # Observability is fail-open and must not affect business execution.
            return


class ProductionTraceSink:
    """A real, provider-neutral sink; external exporters can wrap this contract later."""

    async def start_span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceSpan:
        return SafeTraceSpan(name, attributes)


__all__ = ["ProductionTraceSink", "SafeTraceSpan"]
