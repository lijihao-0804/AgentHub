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

# Server-owned ceiling for newly published long-term-memory-enabled versions.
# It is deliberately outside DEFAULT_CONTEXT_BUDGET so memory-off versions keep
# their historical resolved-spec hash and shared evidence semantics.
DEFAULT_MEMORY_TOKENS = 1_500

MAX_CONTEXT_BUDGET: dict[str, int] = {
    "reserved_output_tokens": 128_000,
    "max_retrieval_tokens": 128_000,
    "max_tool_result_tokens": 128_000,
}

# Memory is opt-in per agent, and every flag defaults off.
#
# Off by default is not timidity. Both flags change what the model is given
# without the operator asking for it on this turn: one lets the agent reach back
# into a thread the window already dropped, the other lets facts written during
# an earlier run reappear in this one. An agent whose behaviour was measured
# without either must keep behaving that way until someone decides otherwise,
# and an A/B between memory on and memory off is only constructible if off is a
# real, published state rather than the absence of a decision.
#
# The block is also omitted from the published spec entirely when every flag is
# false, so turning nothing on leaves the resolved spec byte-identical to what
# the same draft produced before this key existed.
DEFAULT_MEMORY_CONFIG: dict[str, bool] = {
    "thread_history_search": False,
    "long_term_memory": False,
}

__all__ = [
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_MEMORY_CONFIG",
    "DEFAULT_MEMORY_TOKENS",
    "DEFAULT_RUNTIME_LIMITS",
    "DEFAULT_RUN_COST_LIMIT_MICRO_USD",
    "MAX_CONTEXT_BUDGET",
    "MAX_RUNTIME_LIMITS",
    "MAX_RUN_COST_LIMIT_MICRO_USD",
]
