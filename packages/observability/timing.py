"""Request-local, content-free timings layered over the existing TraceSpan contract."""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from packages.observability.contracts import TraceSpan


@dataclass
class TimingCapture:
    started: float = field(default_factory=time.perf_counter)
    first_visible_ms: float | None = None
    stages: dict[str, list[float]] = field(default_factory=dict)
    provider_attempt_count: int = 0
    known_usage_count: int = 0
    known_cost_count: int = 0
    model_calls: int = 0

    def visible_text(self) -> None:
        if self.first_visible_ms is None:
            self.first_visible_ms = (time.perf_counter() - self.started) * 1000

    def projection(self) -> dict[str, Any]:
        return {
            "clock": "process_monotonic",
            "origin": "evaluation_execution_start",
            "server_ttft_ms": self.first_visible_ms,
            "server_ttft_missing_reason": (
                "no_streamed_visible_text" if self.first_visible_ms is None else None
            ),
            "billing": {
                "collection_complete": self.model_calls > 0,
                "provider_attempt_count": self.provider_attempt_count,
                "known_usage_count": self.known_usage_count,
                "known_cost_count": self.known_cost_count,
            },
            "stage_durations_ms": {key: list(values) for key, values in self.stages.items()},
        }


_capture: ContextVar[TimingCapture | None] = ContextVar("agenthub_timing_capture", default=None)


@contextmanager
def capture_timings() -> Iterator[TimingCapture]:
    capture = TimingCapture()
    token = _capture.set(capture)
    try:
        yield capture
    finally:
        _capture.reset(token)


def mark_visible_text() -> None:
    capture = _capture.get()
    if capture is not None:
        capture.visible_text()


class _TimedSpan:
    def __init__(self, span: TraceSpan, name: str, capture: TimingCapture) -> None:
        self.span, self.name, self.capture = span, name, capture
        self.started = time.perf_counter()
        self.ended = False

    async def end(
        self,
        *,
        attributes: Mapping[str, Any] | None = None,
        status: str = "ok",
        failure_code: str | None = None,
    ) -> None:
        if not self.ended:
            self.ended = True
            if self.name == "model.generate" and attributes is not None:
                attempts = attributes.get("attempt_count")
                if type(attempts) is int and attempts > 0:
                    self.capture.model_calls += 1
                    self.capture.provider_attempt_count += attempts
                    self.capture.known_usage_count += int(
                        attributes.get("total_tokens") is not None
                    )
                    self.capture.known_cost_count += int(
                        attributes.get("estimated_cost") is not None
                    )
            self.capture.stages.setdefault(self.name, []).append(
                (time.perf_counter() - self.started) * 1000
            )
        await self.span.end(attributes=attributes, status=status, failure_code=failure_code)


def timed_span(span: TraceSpan, name: str) -> TraceSpan:
    capture = _capture.get()
    return _TimedSpan(span, name, capture) if capture is not None else span
