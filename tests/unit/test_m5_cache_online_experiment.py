from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from benchmarks.evaluation.deepseek_v41_summary_experiment import DEFAULT_TOKENIZER_PATH
from benchmarks.evaluation.m5_cache_online_experiment import (
    M5_MODEL,
    M5_PLANNED_DISPATCHES,
    M5_PROVIDER,
    SOURCE_DOCUMENT_PATH,
    build_control_prefix_b,
    build_planned_requests,
    build_stable_prefix_a,
    execute_m5_cache_online_experiment,
)
from benchmarks.evaluation.memory_trial_budget import (
    FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED,
    FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED,
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
)
from benchmarks.evaluation.memory_trial_client import PRICE_IDENTITY
from benchmarks.evaluation.thread_run_local_experiment import EXPERIMENT_SCOPE_ID
from packages.agent_runtime.deepseek_v41_counter import DeepseekV41TextCounter
from packages.model_gateway.cache_capability import CacheSupport, PromptCacheCapability
from packages.model_gateway.contracts import (
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)


def test_stable_prefix_a_and_control_prefix_b_satisfy_m5_token_and_byte_contracts() -> None:
    counter = DeepseekV41TextCounter.from_file(DEFAULT_TOKENIZER_PATH)
    source_text = SOURCE_DOCUMENT_PATH.read_text(encoding="utf-8")
    prefix_a_1 = build_stable_prefix_a(source_text)
    prefix_a_2 = build_stable_prefix_a(source_text)
    prefix_b = build_control_prefix_b(source_text)

    assert prefix_a_1 == prefix_a_2
    assert prefix_a_1[:64] != prefix_b[:64]

    planned = build_planned_requests(
        counter,
        source_document_text=source_text,
        run_nonce="unit-test-nonce",
    )
    assert len(planned) == M5_PLANNED_DISPATCHES
    assert [item.phase for item in planned] == [
        "first_observation",
        "warm_same_prefix_1",
        "warm_same_prefix_2",
        "control_different_prefix",
    ]
    assert [item.prefix_id for item in planned] == [
        "prefix_a",
        "prefix_a",
        "prefix_a",
        "prefix_b",
    ]
    assert [item.question_id for item in planned] == ["q1", "q2", "q3", "q1"]
    assert planned[0].prefix_sha256 == planned[1].prefix_sha256 == planned[2].prefix_sha256
    assert planned[3].prefix_sha256 != planned[0].prefix_sha256
    for item in planned:
        assert 1500 <= item.prefix_exact_tokens <= 2500
        assert item.local_exact_input_tokens > item.prefix_exact_tokens


@pytest.mark.asyncio
async def test_execute_m5_cache_online_experiment_reserves_and_settles_four_m5_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "experiment-budget.json"
    ledger_state = {
        "schema_version": 2,
        "currency": "CNY",
        "limit_cny": "30",
        "price_identity": PRICE_IDENTITY,
        "attempts": [
            {
                "attempt_id": "m4-1",
                "stage": "M4",
                "status": "SETTLED",
                "estimated_input_tokens": 32768,
                "requested_output_tokens": 512,
                "upper_cost_cny": "0.069632",
                "charged_or_reserved_cny": "0.0082",
            },
            {
                "attempt_id": "m4-2",
                "stage": "M4",
                "status": "RESERVED_OR_UNKNOWN",
                "estimated_input_tokens": 32768,
                "requested_output_tokens": 512,
                "upper_cost_cny": "0.069632",
                "charged_or_reserved_cny": "0.069632",
            },
        ],
        "local_experiment_policy": {
            "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
            "round_attempts_baseline": 2,
            "round_attempt_limit": FINAL_ROUND_ATTEMPT_LIMIT_AUTHORIZED,
            "round_attempts_used": 2,
            "scope_attempt_limit": FINAL_SCOPE_ATTEMPT_LIMIT_AUTHORIZED,
            "active_scope_id": EXPERIMENT_SCOPE_ID,
            "scope_history": [
                {
                    "scope_id": EXPERIMENT_SCOPE_ID,
                    "created_at": "2026-10-28T13:27:30+00:00",
                    "authorization_ref": "Scope init",
                }
            ],
            "policy_history": [
                {
                    "previous_mode": "BLOCK_ALL_UNRESOLVED",
                    "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "changed_at": "2026-10-28T13:27:30+00:00",
                    "scope_id": EXPERIMENT_SCOPE_ID,
                    "authorization_ref": "Scope init",
                },
                {
                    "previous_mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "changed_at": "2026-10-28T15:39:20+00:00",
                    "scope_id": EXPERIMENT_SCOPE_ID,
                    "authorization_ref": "Raise 9->10",
                    "change_type": "SCOPE_ATTEMPT_LIMIT_INCREASE",
                    "previous_scope_attempt_limit": 9,
                    "scope_attempt_limit": 10,
                },
                {
                    "previous_mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "changed_at": "2026-10-28T16:47:33+00:00",
                    "scope_id": EXPERIMENT_SCOPE_ID,
                    "authorization_ref": "Raise 10/12->16/18",
                    "change_type": "LOCAL_ATTEMPT_LIMIT_INCREASE",
                    "previous_scope_attempt_limit": 10,
                    "scope_attempt_limit": 16,
                    "previous_round_attempt_limit": 12,
                    "round_attempt_limit": 18,
                },
                {
                    "previous_mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "mode": RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
                    "changed_at": "2026-11-01T07:21:59+00:00",
                    "scope_id": EXPERIMENT_SCOPE_ID,
                    "authorization_ref": "Raise 16/18->26/28",
                    "change_type": "FINAL_LOCAL_ATTEMPT_LIMIT_INCREASE",
                    "previous_scope_attempt_limit": 16,
                    "scope_attempt_limit": 26,
                    "previous_round_attempt_limit": 18,
                    "round_attempt_limit": 28,
                },
            ],
        },
    }
    ledger_path.write_text(json.dumps(ledger_state, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setenv(
        "AGENTHUB_RAG_BENCHMARK_BASE_URL",
        "https://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("AGENTHUB_RAG_BENCHMARK_API_KEY", "test-secret")

    counter = DeepseekV41TextCounter.from_file(DEFAULT_TOKENIZER_PATH)

    class FakeAdapter:
        def __init__(self) -> None:
            self.call_index = 0

        @staticmethod
        def prompt_cache_capability(profile, credential) -> PromptCacheCapability:
            del profile, credential
            return PromptCacheCapability(CacheSupport.UNKNOWN, CacheSupport.UNKNOWN)

        async def stream(self, profile, credential, request):
            del profile, credential
            self.call_index += 1
            exact_in = counter.count(request, thinking=False)
            cached = 1984 if self.call_index in {2, 3} else 0
            text = (
                "Changes to stateful operations are not allowed; Avro encoding supports schema "
                "migration. Kafka and file sinks have specific rules. asyncProgressTrackingEnabled "
                "defaults to false and interval is 1000 ms."
            )
            usage = ModelUsage(
                input_tokens=exact_in,
                output_tokens=40,
                total_tokens=exact_in + 40,
                cached_tokens=cached,
            )
            response = ModelResponse(
                content=text,
                provider=M5_PROVIDER,
                model=M5_MODEL,
                finish_reason="stop",
                usage=usage,
            )
            yield ModelStreamEvent(ModelStreamEventType.MESSAGE_DELTA, message_delta=text)
            yield ModelStreamEvent(ModelStreamEventType.USAGE, usage=usage)
            yield ModelStreamEvent(ModelStreamEventType.COMPLETED, response=response)

    evidence_dir = tmp_path / "evidence"
    result = await execute_m5_cache_online_experiment(
        ledger_path=ledger_path,
        tokenizer_path=DEFAULT_TOKENIZER_PATH,
        evidence_dir=evidence_dir,
        inter_request_pause_seconds=0.0,
        adapter=FakeAdapter(),
        run_nonce="unittest01",
    )

    assert result["summary"]["total_requests"] == 4
    assert result["summary"]["all_settled"] is True
    assert result["summary"]["all_answers_correct"] is True
    assert result["summary"]["all_exact_token_counts_matched"] is True
    assert result["summary"]["warm_cache_hit_observed"] is True
    assert result["summary"]["control_prefix_isolated"] is True
    assert result["ledger"]["attempt_count"] == 6
    assert result["ledger"]["m5_attempt_count"] == 4
    assert Decimal(result["summary"]["total_peak_estimated_cost_cny"]) > Decimal("0")
    assert (evidence_dir / "m5-implicit-cache-online-evidence.json").is_file()
