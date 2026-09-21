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

# A per-run spend ceiling, in micro-USD (1_000_000 == USD 1.00).
#
# Integer micro-USD rather than a fractional dollar amount because this value is
# frozen into a published agent version and hashed: an integer has exactly one
# representation, a float has several, and a spend limit is the last place where
# rounding should be a matter of opinion.
#
# ``None`` means no ceiling, which is what every existing agent has: the limit
# is opt-in, so nothing published before it existed changes behaviour.
DEFAULT_RUN_COST_LIMIT_MICRO_USD: int | None = None
MAX_RUN_COST_LIMIT_MICRO_USD = 100_000_000  # USD 100 per run

DEFAULT_CONTEXT_BUDGET: dict[str, int] = {
    "reserved_output_tokens": 2_000,
    "max_retrieval_tokens": 5_000,
    "max_tool_result_tokens": 4_000,
}

MAX_CONTEXT_BUDGET: dict[str, int] = {
    "reserved_output_tokens": 128_000,
    "max_retrieval_tokens": 128_000,
    "max_tool_result_tokens": 128_000,
}

__all__ = [
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_RUNTIME_LIMITS",
    "DEFAULT_RUN_COST_LIMIT_MICRO_USD",
    "MAX_CONTEXT_BUDGET",
    "MAX_RUNTIME_LIMITS",
    "MAX_RUN_COST_LIMIT_MICRO_USD",
]
