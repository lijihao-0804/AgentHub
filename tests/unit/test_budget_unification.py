"""Budget unification batch 1: unit alignment, utilization, derivation.

Work order: docs/reviews/AgentHub-上下文预算统一修复工作单-0730a94.md.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.agent_runtime.runtime_config import (
    DEFAULT_CONTEXT_BUDGET,
    derive_context_budget,
    derive_memory_tokens,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


def _profile(*, context: int, max_tokens: int = 16) -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="test",
        model="test-model",
        temperature=Decimal(0),
        max_tokens=max_tokens,
        timeout_seconds=Decimal(30),
        capabilities=ModelCapabilities(max_context_tokens=context),
    )


def _policy(context: int, config: ContextBudgetConfig) -> ContextBudgetPolicy:
    spec = FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(
            primary=_profile(context=context), retry_policy=RetryPolicy()
        ),
        system_prompt="system",
        prompt_version=1,
        runtime={},
    )
    return ContextBudgetPolicy(spec, config, estimator=ApproxTokenEstimator())


# ---------------------------------------------------------------------------
# FIX-01: language-neutral accounting
# ---------------------------------------------------------------------------


def test_equivalent_content_costs_the_same_magnitude_in_both_languages() -> None:
    estimator = ApproxTokenEstimator()
    chinese = "退款" * 600  # 1200 CJK chars
    english = "refund " * 300  # 2100 ASCII chars, similar content volume

    zh_units = estimator.estimate(chinese)
    en_units = estimator.estimate(english)

    # Old byte accounting charged 3600 vs 2100 (1.7x) for equal characters
    # and up to 5x for equivalent meaning; approx tokens land the same
    # order of magnitude, which is the acceptance the work order asks for.
    assert 700 <= zh_units <= 800
    assert 450 <= en_units <= 550
    assert zh_units / en_units < 2.0


def test_approx_units_are_3_to_5x_smaller_than_legacy_bytes() -> None:
    estimator = ApproxTokenEstimator()
    chinese = "退款政策手册" * 200
    english = "refund policy handbook " * 100

    assert 3.0 <= len(chinese.encode("utf-8")) / estimator.estimate(chinese) <= 5.0
    assert 3.0 <= len(english.encode()) / estimator.estimate(english) <= 5.0


# ---------------------------------------------------------------------------
# FIX-02: unit alignment and window utilization
# ---------------------------------------------------------------------------


def test_sixteen_k_window_admits_twelve_k_true_token_english_content() -> None:
    # Terminal-report experiment 2 as a regression test: 12k true tokens of
    # English content must fit a 16k window (byte accounting rejected it).
    content = "word " * 9_600  # 48k chars -> 12k approx tokens
    policy = _policy(
        16_384,
        ContextBudgetConfig(
            reserved_output_tokens=2_000,
            max_retrieval_tokens=4_096,
            max_tool_result_tokens=12_000,
        ),
    )

    admission = policy.admit(
        (
            ContextMessage(
                ModelMessage(role="user", content=content),
                ContextCategory.CURRENT_USER_TASK,
            ),
        )
    )

    assert admission.usage.estimated_input_after >= 10_000
    assert not admission.usage.truncated


def test_full_derived_pools_reach_their_window_share() -> None:
    window = 32_768
    derived = derive_context_budget(window)
    # Fill both pools to their ceiling minus the per-message metadata the
    # pools must also carry; the ceiling of derived shares is 25%+15% = 40%
    # of the window before metadata, so the honest floor sits just below.
    evidence = "证" * int((derived["max_retrieval_tokens"] - 64) * 1.6)
    tool_result = "x" * int((derived["max_tool_result_tokens"] - 64) * 4)
    policy = _policy(
        window,
        ContextBudgetConfig(
            reserved_output_tokens=derived["reserved_output_tokens"],
            max_retrieval_tokens=derived["max_retrieval_tokens"],
            max_tool_result_tokens=derived["max_tool_result_tokens"],
        ),
    )

    from packages.model_gateway.contracts import ModelToolCall

    admission = policy.admit(
        (
            ContextMessage(
                ModelMessage(role="user", content="question"),
                ContextCategory.CURRENT_USER_TASK,
            ),
            ContextMessage(
                ModelMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        ModelToolCall(name="search_knowledge", arguments={}),
                    ),
                ),
                ContextCategory.CONVERSATION,
                "exchange-1",
            ),
            ContextMessage(
                ModelMessage(
                    role="tool", name="search_knowledge", tool_call_id="c1", content=evidence
                ),
                ContextCategory.RAG_EVIDENCE,
                "exchange-1",
            ),
            ContextMessage(
                ModelMessage(
                    role="assistant",
                    content=None,
                    tool_calls=(
                        ModelToolCall(name="lookup", arguments={}),
                    ),
                ),
                ContextCategory.CONVERSATION,
                "exchange-2",
            ),
            ContextMessage(
                ModelMessage(role="tool", name="lookup", tool_call_id="c2", content=tool_result),
                ContextCategory.TOOL_RESULT,
                "exchange-2",
            ),
        ),
        exchange_groups=[None, "exchange-1", "exchange-1", "exchange-2", "exchange-2"],
    )

    assert admission.usage.window_utilization >= 0.38
    assert admission.usage.estimated_input_after > 12_000


# ---------------------------------------------------------------------------
# FIX-03: window-derived pools
# ---------------------------------------------------------------------------


def test_pools_derive_from_window_shares() -> None:
    assert derive_context_budget(32_768)["max_retrieval_tokens"] == 8_192
    assert derive_context_budget(32_768)["max_tool_result_tokens"] == 4_915
    assert derive_context_budget(16_384)["max_retrieval_tokens"] == 4_096
    assert derive_memory_tokens(32_768) == 2_621
    # The legacy fixed defaults stay as the floor baseline, not the answer.
    assert DEFAULT_CONTEXT_BUDGET["max_retrieval_tokens"] == 5_000


def test_publish_derives_pools_for_default_config_and_keeps_explicit_values() -> None:
    from packages.agent_runtime.publish import _resolved_runtime_config

    model = (
        (
            _profile(context=32_768),
            _profile(context=16_384),  # fallback: the chain minimum binds
        ),
        {},
    )

    derived = _resolved_runtime_config(
        {"context_budget": dict(DEFAULT_CONTEXT_BUDGET)}, model
    )
    assert derived["context_budget"]["max_retrieval_tokens"] == 4_096  # min window x 25%
    assert derived["context_budget"]["max_tool_result_tokens"] == 2_457

    explicit = _resolved_runtime_config(
        {"context_budget": {**DEFAULT_CONTEXT_BUDGET, "max_retrieval_tokens": 3_000}},
        model,
    )
    assert explicit["context_budget"]["max_retrieval_tokens"] == 3_000  # explicit wins


def test_generic_tool_projection_keeps_whole_records_not_a_sliced_preview() -> None:
    from packages.agent_runtime.runtime import _bounded_tool_result
    from packages.tools.contracts import ToolResult

    records = {"results": [{"id": index, "text": "row " * 50} for index in range(5)]}
    bounded = _bounded_tool_result(ToolResult.success(records), max_tool_result_tokens=150)

    kept = bounded["data"]["results"]
    assert kept and all(isinstance(item, dict) and item in records["results"] for item in kept)
    assert bounded["data"].get("truncated") is True
    assert "preview" not in bounded["data"]
    assert bounded.get("dropped_results") == 5 - len(kept)


def test_search_knowledge_default_limit_is_ten_ceiling() -> None:
    from packages.tools.builtins.search_knowledge import _MAX_RESULTS

    assert _MAX_RESULTS == 10
