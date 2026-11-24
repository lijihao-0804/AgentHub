"""Unit tests for provider-feedback token calibration and overflow recovery."""

from __future__ import annotations

import pytest

from packages.agent_runtime.context_budget import ApproxTokenEstimator, ContextBudgetConfig
from packages.agent_runtime.context_calibration import (
    MAX_CALIBRATION_SCALE,
    MIN_CALIBRATION_SCALE,
    CalibratedTokenEstimator,
    is_context_overflow_error,
    tightened_budget_config,
    update_calibration_scale,
)


def test_update_calibration_scale_adjusts_upward_and_clamps() -> None:
    # Underestimated by 30%: actual=1300 vs estimated=1000
    s1 = update_calibration_scale(
        previous_scale=1.0,
        estimated_input_tokens=1000,
        actual_input_tokens=1300,
    )
    assert s1 == pytest.approx(1.21)

    # Second round converges closer to 1.30 (estimated now includes 1.21x scaling)
    s2 = update_calibration_scale(
        previous_scale=s1,
        estimated_input_tokens=1210,
        actual_input_tokens=1300,
    )
    assert s2 == pytest.approx(1.273)

    # Extreme underestimation clamps at MAX_CALIBRATION_SCALE
    s_max = update_calibration_scale(
        previous_scale=1.0,
        estimated_input_tokens=100,
        actual_input_tokens=900,
    )
    assert s_max == MAX_CALIBRATION_SCALE

    # Extreme overestimation clamps at MIN_CALIBRATION_SCALE
    s_min = update_calibration_scale(
        previous_scale=1.0,
        estimated_input_tokens=1000,
        actual_input_tokens=200,
    )
    assert s_min == MIN_CALIBRATION_SCALE


def test_update_calibration_scale_ignores_small_drift_or_missing_usage() -> None:
    assert (
        update_calibration_scale(
            previous_scale=1.15,
            estimated_input_tokens=1150,
            actual_input_tokens=None,
        )
        == 1.15
    )
    # Within 5% drift: stays stable
    assert (
        update_calibration_scale(
            previous_scale=1.0,
            estimated_input_tokens=1000,
            actual_input_tokens=1030,
        )
        == 1.0
    )


def test_calibrated_token_estimator_scales_base_estimate() -> None:
    base = ApproxTokenEstimator()
    text = "def compute_metrics(values): return sum(values) / len(values)"
    raw = base.estimate(text)
    scaled = CalibratedTokenEstimator(base, scale=1.25).estimate(text)
    assert scaled > raw
    assert CalibratedTokenEstimator(base, scale=1.0).estimate(text) == raw
    assert CalibratedTokenEstimator(base, scale=1.5).estimate("") == 0


def test_is_context_overflow_error_and_tightened_budget_config() -> None:
    assert is_context_overflow_error(RuntimeError("Error code: context_length_exceeded"))
    assert is_context_overflow_error(ValueError("This model's maximum context length is 65536"))
    assert not is_context_overflow_error(RuntimeError("rate limit exceeded"))

    cfg = ContextBudgetConfig(
        reserved_output_tokens=2000,
        max_retrieval_tokens=8000,
        max_tool_result_tokens=4000,
        max_memory_tokens=2000,
    )
    tight = tightened_budget_config(cfg, factor=0.75)
    assert tight.reserved_output_tokens == 2000
    assert tight.max_retrieval_tokens == 6000
    assert tight.max_tool_result_tokens == 3000
    assert tight.max_memory_tokens == 1500
