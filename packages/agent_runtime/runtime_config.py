"""Shared server-owned limits for immutable agent runtime configuration."""

from __future__ import annotations

DEFAULT_RUNTIME_LIMITS: dict[str, int] = {
    "max_steps": 8,
    "max_tool_calls": 12,
    "max_identical_calls": 2,
    "max_parallel_reads": 3,
}

MAX_RUNTIME_LIMITS: dict[str, int] = {
    "max_steps": 32,
    "max_tool_calls": 64,
    "max_identical_calls": 4,
    "max_parallel_reads": 8,
}

DEFAULT_CONTEXT_BUDGET: dict[str, int] = {
    "reserved_output_tokens": 2_000,
    "max_retrieval_tokens": 5_000,
    "max_tool_result_tokens": 4_000,
}

__all__ = ["DEFAULT_CONTEXT_BUDGET", "DEFAULT_RUNTIME_LIMITS", "MAX_RUNTIME_LIMITS"]
