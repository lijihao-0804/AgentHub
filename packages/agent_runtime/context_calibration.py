"""Provider-feedback token calibration and pre-token context overflow recovery.

Offline heuristics such as ``ApproxTokenEstimator`` can underestimate dense code,
escaped JSON, or domain tokens. This module provides:

1. ``CalibratedTokenEstimator``: wraps any ``TokenEstimator`` with a bounded
   multiplicative calibration factor learned from real provider
   ``usage.input_tokens`` vs estimated tokens in prior rounds of the same run.
2. ``update_calibration_scale``: computes the next bounded scale factor
   (clamped to ``[0.85, 2.0]``; defaults to ``1.0`` when no real usage exists).
3. ``is_context_overflow_error`` & ``tightened_budget_config``: detects upstream
   context-length-exceeded errors and produces a tightened ``ContextBudgetConfig``
   for a single safe pre-visible-token retry (rule 15).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextBudgetConfig,
    TokenEstimator,
)

MIN_CALIBRATION_SCALE = 0.85
MAX_CALIBRATION_SCALE = 2.0
DEFAULT_TIGHTEN_FACTOR = 0.75
_MIN_TIGHTENED_POOL = 256

_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
    "too many tokens",
    "input length exceeds",
    "exceeds the maximum number of tokens",
)


def update_calibration_scale(
    *,
    previous_scale: float,
    estimated_input_tokens: int,
    actual_input_tokens: int | None,
) -> float:
    """Compute the next bounded calibration multiplier from provider feedback.

    Uses exponential smoothing (70% new observation, 30% previous) when a valid
    provider ``actual_input_tokens`` is reported, and clamps to
    ``[MIN_CALIBRATION_SCALE, MAX_CALIBRATION_SCALE]`` so a single noisy reading
    cannot collapse or explode budgets.
    """
    base = previous_scale if previous_scale > 0 else 1.0
    if (
        actual_input_tokens is None
        or actual_input_tokens <= 0
        or estimated_input_tokens <= 0
    ):
        return round(min(max(base, MIN_CALIBRATION_SCALE), MAX_CALIBRATION_SCALE), 4)
    # Undo previous scaling to measure raw estimator ratio: actual / raw_estimated.
    raw_estimated = estimated_input_tokens / base
    if raw_estimated <= 0:
        return round(min(max(base, MIN_CALIBRATION_SCALE), MAX_CALIBRATION_SCALE), 4)
    observed = actual_input_tokens / raw_estimated
    # Only blend when there is meaningful drift (>5%), otherwise keep stable.
    if abs(observed - base) < 0.05:
        return round(min(max(base, MIN_CALIBRATION_SCALE), MAX_CALIBRATION_SCALE), 4)
    blended = 0.7 * observed + 0.3 * base
    clamped = min(max(blended, MIN_CALIBRATION_SCALE), MAX_CALIBRATION_SCALE)
    return round(clamped, 4)


@dataclass(frozen=True, slots=True)
class CalibratedTokenEstimator:
    """Deterministic wrapper applying a bounded calibration scale to an estimator."""

    base: TokenEstimator = ApproxTokenEstimator()
    scale: float = 1.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise ValueError("calibration scale must be a positive finite number")

    def estimate(self, text: str) -> int:
        raw = self.base.estimate(text)
        if raw <= 0 or self.scale == 1.0:
            return raw
        return max(1, math.ceil(raw * self.scale))


def is_context_overflow_error(error: BaseException) -> bool:
    """Return True if an upstream error indicates the prompt exceeded model context."""
    text = f"{type(error).__name__} {error}".lower()
    code = str(getattr(error, "code", "") or "").lower()
    combined = f"{code} {text}"
    return any(marker in combined for marker in _OVERFLOW_MARKERS)


def tightened_budget_config(
    config: ContextBudgetConfig,
    *,
    factor: float = DEFAULT_TIGHTEN_FACTOR,
) -> ContextBudgetConfig:
    """Return a tighter ContextBudgetConfig for a single pre-visible-token retry."""
    if not 0.1 <= factor < 1.0:
        raise ValueError("tighten factor must be in [0.1, 1.0)")

    def _shrink(value: int) -> int:
        if value <= _MIN_TIGHTENED_POOL:
            return value
        return max(_MIN_TIGHTENED_POOL, int(value * factor))

    return replace(
        config,
        max_retrieval_tokens=_shrink(config.max_retrieval_tokens),
        max_tool_result_tokens=_shrink(config.max_tool_result_tokens),
        max_memory_tokens=(
            None if config.max_memory_tokens is None else _shrink(config.max_memory_tokens)
        ),
    )


__all__ = [
    "CalibratedTokenEstimator",
    "DEFAULT_TIGHTEN_FACTOR",
    "MAX_CALIBRATION_SCALE",
    "MIN_CALIBRATION_SCALE",
    "is_context_overflow_error",
    "tightened_budget_config",
    "update_calibration_scale",
]
