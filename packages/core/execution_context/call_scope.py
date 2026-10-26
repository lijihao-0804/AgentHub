"""Task-local deadlines and auxiliary model accounting, without business data."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_deadline: ContextVar[float | None] = ContextVar("agenthub_call_deadline", default=None)


@contextmanager
def execution_deadline(seconds: float):
    cutoff = time.monotonic() + seconds
    inherited = _deadline.get()
    token = _deadline.set(min(cutoff, inherited) if inherited is not None else cutoff)
    try:
        yield _deadline.get()
    finally:
        _deadline.reset(token)


def remaining_call_seconds() -> float | None:
    cutoff = _deadline.get()
    return None if cutoff is None else max(0.0, cutoff - time.monotonic())


@dataclass
class AuxiliaryModelCalls:
    allow_unpriced: bool
    records: list[dict[str, Any]] = field(default_factory=list)


_calls: ContextVar[AuxiliaryModelCalls | None] = ContextVar(
    "agenthub_auxiliary_calls", default=None
)
_attempt: ContextVar[dict[str, Any] | None] = ContextVar("agenthub_auxiliary_attempt", default=None)


@contextmanager
def capture_auxiliary_model_calls(*, allow_unpriced: bool):
    calls = AuxiliaryModelCalls(allow_unpriced=allow_unpriced)
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


def unpriced_auxiliary_calls_allowed() -> bool:
    calls = _calls.get()
    return calls is None or calls.allow_unpriced


@contextmanager
def auxiliary_model_attempt(*, source: str, provider: str, model: str | None):
    record = {
        "source": source,
        "provider": provider,
        "model": model,
        "usage": None,
        "cost": None,
        "attempt_status": "UNKNOWN",
    }
    calls = _calls.get()
    if calls is not None:
        if not calls.allow_unpriced:
            raise ValueError("Unpriced auxiliary model dispatch is forbidden")
        calls.records.append(record)
    token = _attempt.set(record)
    try:
        yield record
    finally:
        _attempt.reset(token)


def report_auxiliary_model_usage(*, model: str | None, usage: dict | None):
    record = _attempt.get()
    if record is not None:
        record["model"] = model if isinstance(model, str) and len(model) <= 256 else None
        record["usage"] = usage
