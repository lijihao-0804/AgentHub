"""Bounded M5 online experiment for Aliyun Beijing DeepSeek V4.1 implicit cache.

Executes the 4-request protocol from Section 4.3 of the M3-M5 execution plan:
1. Request 1 (first observation): Prefix A + Question 1
2. Request 2 (warm request 1):   Prefix A + Question 2
3. Request 3 (warm request 2):   Prefix A + Question 3
4. Request 4 (control prefix):   Prefix B + Question 1

All dispatches use NON_THINKING mode, num_retries=0, no fallback, exact local
DeepseekV41TextCounter verification, and atomic reservation/settlement in the
shared research ledger (stage="M5").
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks.evaluation.deepseek_v41_summary_experiment import DEFAULT_TOKENIZER_PATH
from benchmarks.evaluation.memory_trial_budget import (
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
    MemoryTrialBudget,
    upper_bound_cost_cny,
)
from benchmarks.evaluation.memory_trial_client import PRICE_IDENTITY
from benchmarks.evaluation.thread_run_local_experiment import (
    DEFAULT_ENV_FILE,
    DEFAULT_LEDGER_PATH,
    EXPERIMENT_SCOPE_ID,
    is_allowlisted_beijing_url,
    load_config,
)
from packages.agent_runtime.deepseek_v41_counter import DeepseekV41TextCounter
from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_ID,
    SUMMARY_NON_THINKING_COUNTER_REVISION,
)
from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.model_gateway.adapters.litellm.adapter import (
    LiteLLMProviderAdapter,
    _tool_arguments_payload,
)
from packages.model_gateway.cache_capability import CacheSupport, prompt_cache_capability
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelMessage,
    ModelRequest,
    ModelStreamEventType,
    ModelUsage,
    RetryPolicy,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DOCUMENT_PATH = (
    WORKTREE_ROOT
    / "benchmarks"
    / "evaluation"
    / "local_memory_pilot_20261028"
    / "data"
    / "source_document.md"
)
DEFAULT_M5_EVIDENCE_DIR = (
    WORKTREE_ROOT / "docs" / "reviews" / "evidence" / "context-policy-m5-20261029"
)

M5_PROVIDER = "openai-compatible"
M5_MODEL = "deepseek-v4.1-flash-1017"
M5_STAGE = "M5"
M5_REQUESTED_OUTPUT_TOKENS = 256
M5_WINDOW_INPUT_BOUND = 32768
M5_MIN_PREFIX_TOKENS = 1024
M5_MAX_PREFIX_TOKENS = 2500
M5_PLANNED_DISPATCHES = 4

FROZEN_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "inspect_checkpoint_policy",
        "description": "Read-only lookup for Structured Streaming checkpointing rules.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["section", "operation_kind"],
            "properties": {
                "operation_kind": {
                    "type": "string",
                    "enum": [
                        "aggregation",
                        "deduplication",
                        "stream_stream_join",
                        "map_groups_with_state",
                    ],
                },
                "section": {"type": "string", "minLength": 1},
            },
        },
    },
    {
        "name": "verify_sink_compatibility",
        "description": "Read-only check for allowed source and sink transitions across restarts.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["previous_sink", "next_sink"],
            "properties": {
                "next_sink": {"type": "string", "enum": ["kafka", "file", "foreach", "memory"]},
                "previous_sink": {
                    "type": "string",
                    "enum": ["kafka", "file", "foreach", "memory"],
                },
            },
        },
    },
    {
        "name": "inspect_async_progress_tracking",
        "description": "Read-only lookup for Asynchronous Progress Tracking options and defaults.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["option_name"],
            "properties": {
                "option_name": {
                    "type": "string",
                    "enum": [
                        "asyncProgressTrackingEnabled",
                        "asyncProgressTrackingCheckpointIntervalMs",
                    ],
                }
            },
        },
    },
)

FROZEN_HISTORICAL_TOOL_CALL_SAMPLE: dict[str, Any] = {
    "zebra_option": "asyncProgressTrackingCheckpointIntervalMs",
    "alpha_section": "Asynchronous Progress Tracking",
    "nested": {"delta_Sink": "kafka", "beta_Source": "kafka"},
}

CONTROL_DIFFERENTIAL_PREAMBLE = (
    "Control Prefix Variant B — Independent Prefix Sequence for Cache Isolation Check.\n"
    "In Apache Spark 3.5.9 Structured Streaming, queries are processed by default using a "
    "micro-batch processing engine that achieves end-to-end latencies as low as 100 milliseconds "
    "and exactly-once fault-tolerance guarantees through checkpointing and write-ahead logs. "
    "Continuous Processing mode, introduced in Spark 2.3, provides low-latency processing down to "
    "1 millisecond with at-least-once fault-tolerance guarantees. When comparing prefix caching "
    "across requests, any divergence in the leading token blocks of the system message must "
    "prevent prefix sharing with Prefix Variant A while keeping the reference corpus intact.\n"
)

QUESTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "q1",
        (
            "According to the provided Spark documentation, which changes to stateful operations "
            "(such as streaming aggregation, deduplication, or stream-stream join) are allowed or "
            "not allowed between restarts from the same checkpoint location, and how can Avro "
            "encoding help?"
        ),
        ("not allowed", "Avro"),
    ),
    (
        "q2",
        (
            "According to the provided Spark documentation, which changes between input sources "
            "and output sinks (specifically Kafka vs file sink, rate limits, and output topics) "
            "are allowed or not allowed when restarting a streaming query?"
        ),
        ("Kafka", "file"),
    ),
    (
        "q3",
        (
            "According to the provided Spark documentation, what is Asynchronous Progress "
            "Tracking, which two configuration options control it, what are their default values, "
            "and which sinks are initially supported?"
        ),
        ("asyncProgressTrackingEnabled", "1000"),
    ),
)


@dataclass(frozen=True, slots=True)
class M5PlannedRequest:
    request_index: int
    phase: str
    prefix_id: str
    prefix_text: str
    prefix_sha256: str
    prefix_exact_tokens: int
    question_id: str
    question_text: str
    expected_keywords: tuple[str, ...]
    model_request: ModelRequest
    local_exact_input_tokens: int
    request_identity: str


def _extract_reference_excerpt(source_document_text: str) -> str:
    marker = "## Recovery Semantics after Changes in a Streaming Query"
    index = source_document_text.find(marker)
    excerpt = source_document_text[index:] if index >= 0 else source_document_text
    return excerpt.strip()


def build_stable_prefix_a(source_document_text: str) -> str:
    """Build deterministic Prefix A (>1024 tokens) from fixed instructions and public text."""
    serialized_tools = canonical_json({"tools": list(FROZEN_TOOL_SCHEMAS)})
    serialized_args = _tool_arguments_payload(FROZEN_HISTORICAL_TOOL_CALL_SAMPLE)
    excerpt = _extract_reference_excerpt(source_document_text)
    return (
        "System Instruction (Prefix Variant A - Deterministic Prefix Contract):\n"
        "You are a controlled technical documentation assistant. Answer the user's question "
        "strictly using the Apache Spark 3.5.9 Structured Streaming documentation and the "
        "deterministic tool schema catalog below. Do not invent unsupported options. Reply "
        "concisely in 3 to 5 sentences in English.\n\n"
        f"Frozen Tool Schema Catalog (canonical JSON):\n{serialized_tools}\n"
        f"Deterministic Historical Tool Call Arguments Sample:\n{serialized_args}\n\n"
        "Public Reference Excerpt (Apache Spark 3.5.9 Structured Streaming Guide, Apache-2.0):\n"
        f"{excerpt}"
    )


def build_control_prefix_b(source_document_text: str) -> str:
    """Build Control Prefix B (>1024 tokens) that diverges from byte 0 while keeping corpus."""
    serialized_tools = canonical_json({"tools": list(reversed(FROZEN_TOOL_SCHEMAS))})
    excerpt = _extract_reference_excerpt(source_document_text)
    return (
        f"{CONTROL_DIFFERENTIAL_PREAMBLE}\n"
        "System Instruction (Prefix Variant B - Control Sequence):\n"
        "You are a controlled technical documentation assistant. Answer the user's question "
        "strictly using the Apache Spark 3.5.9 Structured Streaming documentation below. "
        "Do not invent unsupported options. Reply concisely in 3 to 5 sentences in English.\n\n"
        "Public Reference Excerpt (Apache Spark 3.5.9 Structured Streaming Guide, Apache-2.0):\n"
        f"{excerpt}\n\n"
        f"Control Tool Catalog (canonical JSON):\n{serialized_tools}"
    )


def build_planned_requests(
    counter: DeepseekV41TextCounter,
    *,
    source_document_text: str,
    run_nonce: str,
) -> tuple[M5PlannedRequest, ...]:
    """Construct the exact 4-request cold/warm/control sequence and verify token bounds."""
    prefix_a = build_stable_prefix_a(source_document_text)
    prefix_b = build_control_prefix_b(source_document_text)
    if prefix_a[:64] == prefix_b[:64]:
        raise ValueError("M5_CONTROL_PREFIX_LEADING_BYTES_NOT_DISTINCT")

    prefix_a_tokens = counter.count(
        ModelRequest(
            messages=(ModelMessage("system", prefix_a), ModelMessage("user", ".")),
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        ),
        thinking=False,
    )
    prefix_b_tokens = counter.count(
        ModelRequest(
            messages=(ModelMessage("system", prefix_b), ModelMessage("user", ".")),
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        ),
        thinking=False,
    )
    if not (M5_MIN_PREFIX_TOKENS < prefix_a_tokens <= M5_MAX_PREFIX_TOKENS):
        raise ValueError(f"M5_PREFIX_A_TOKENS_OUT_OF_RANGE:{prefix_a_tokens}")
    if not (M5_MIN_PREFIX_TOKENS < prefix_b_tokens <= M5_MAX_PREFIX_TOKENS):
        raise ValueError(f"M5_PREFIX_B_TOKENS_OUT_OF_RANGE:{prefix_b_tokens}")

    q_map = {qid: (qtext, keywords) for qid, qtext, keywords in QUESTIONS}
    schedule = (
        (1, "first_observation", "prefix_a", prefix_a, prefix_a_tokens, "q1"),
        (2, "warm_same_prefix_1", "prefix_a", prefix_a, prefix_a_tokens, "q2"),
        (3, "warm_same_prefix_2", "prefix_a", prefix_a, prefix_a_tokens, "q3"),
        (4, "control_different_prefix", "prefix_b", prefix_b, prefix_b_tokens, "q1"),
    )
    planned: list[M5PlannedRequest] = []
    for index, phase, prefix_id, prefix_text, prefix_tokens, qid in schedule:
        qtext, keywords = q_map[qid]
        request = ModelRequest(
            messages=(
                ModelMessage("system", prefix_text),
                ModelMessage("user", qtext),
            ),
            retry_policy=RetryPolicy(max_attempts=1),
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        )
        exact_tokens = counter.count(request, thinking=False)
        prefix_sha = hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()
        request_identity = canonical_json_hash(
            {
                "version": "m5-cache-online-request-v1",
                "run_nonce": run_nonce,
                "request_index": index,
                "phase": phase,
                "prefix_id": prefix_id,
                "prefix_sha256": prefix_sha,
                "question_id": qid,
                "local_exact_input_tokens": exact_tokens,
                "requested_output_tokens": M5_REQUESTED_OUTPUT_TOKENS,
                "model": M5_MODEL,
                "provider": M5_PROVIDER,
                "request_mode": "non_thinking",
            }
        )
        planned.append(
            M5PlannedRequest(
                request_index=index,
                phase=phase,
                prefix_id=prefix_id,
                prefix_text=prefix_text,
                prefix_sha256=prefix_sha,
                prefix_exact_tokens=prefix_tokens,
                question_id=qid,
                question_text=qtext,
                expected_keywords=keywords,
                model_request=request,
                local_exact_input_tokens=exact_tokens,
                request_identity=request_identity,
            )
        )
    return tuple(planned)


async def execute_m5_cache_online_experiment(
    *,
    ledger_path: Path = DEFAULT_LEDGER_PATH,
    tokenizer_path: Path = DEFAULT_TOKENIZER_PATH,
    evidence_dir: Path = DEFAULT_M5_EVIDENCE_DIR,
    inter_request_pause_seconds: float = 1.5,
    adapter: LiteLLMProviderAdapter | None = None,
    run_nonce: str | None = None,
) -> dict[str, Any]:
    """Execute the 4-request M5 online implicit cache experiment with atomic ledger accounting."""
    config = load_config(
        env_file=DEFAULT_ENV_FILE,
        ledger_path=ledger_path,
        tokenizer_path=tokenizer_path,
    )
    if not is_allowlisted_beijing_url(config.base_url):
        raise ValueError("M5_BEIJING_BASE_URL_REQUIRED")
    if not config.api_key:
        raise ValueError("M5_API_KEY_REQUIRED")
    source_text = SOURCE_DOCUMENT_PATH.read_text(encoding="utf-8")
    counter = DeepseekV41TextCounter.from_file(tokenizer_path)
    nonce = run_nonce or uuid4().hex[:12]
    planned = build_planned_requests(
        counter,
        source_document_text=source_text,
        run_nonce=nonce,
    )

    profile = ModelProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        model=M5_MODEL,
        temperature=Decimal("0.0"),
        max_tokens=M5_REQUESTED_OUTPUT_TOKENS,
        timeout_seconds=Decimal("60"),
        fallback_profile_id=None,
        capabilities={
            "tool_calling": False,
            "streaming": True,
            "structured_output": False,
            "max_context_tokens": 32768,
        },
        enabled=True,
    )
    credential = ProviderCredential(
        id=profile.provider_credential_id,
        workspace_id=profile.workspace_id,
        provider=M5_PROVIDER,
        name="m5-cache-online-credential",
        secret=config.api_key,
        base_url=config.base_url,
        enabled=True,
    )
    provider_adapter = adapter or LiteLLMProviderAdapter()
    capability = provider_adapter.prompt_cache_capability(profile, credential)
    direct_capability = prompt_cache_capability(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model=M5_MODEL,
    )
    if direct_capability.implicit is not CacheSupport.SUPPORTED:
        raise ValueError("M5_IMPLICIT_CACHE_CAPABILITY_UNEXPECTED")

    upper_cost = upper_bound_cost_cny(
        input_token_bound=M5_WINDOW_INPUT_BOUND,
        output_token_bound=M5_REQUESTED_OUTPUT_TOKENS,
        input_per_million=Decimal("2"),
        output_per_million=Decimal("8"),
    )

    records: list[dict[str, Any]] = []
    with MemoryTrialBudget(ledger_path, price_identity=PRICE_IDENTITY) as budget:
        policy = budget.state.get("local_experiment_policy", {})
        if policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT:
            raise ValueError("M5_LEDGER_POLICY_INVALID")
        if policy.get("active_scope_id") != EXPERIMENT_SCOPE_ID:
            raise ValueError("M5_LEDGER_SCOPE_MISMATCH")

        for item in planned:
            attempt_id = f"m5-{nonce}-req{item.request_index}"
            input_counter_record = {
                "counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
                "tokenizer_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
                "thinking_mode": "NON_THINKING",
                "tokens": item.local_exact_input_tokens,
                "input_limit": 6144,
                "request_identity": item.request_identity,
            }
            reservation_receipt = budget.reserve(
                attempt_id,
                stage=M5_STAGE,
                estimated_input_tokens=M5_WINDOW_INPUT_BOUND,
                requested_output_tokens=M5_REQUESTED_OUTPUT_TOKENS,
                upper_cost_cny=upper_cost,
                input_counter=input_counter_record,
                request_identity=item.request_identity,
            )

            started_ns = time.perf_counter_ns()
            first_token_ns: int | None = None
            final_response = None
            final_usage: ModelUsage | None = None
            async for event in provider_adapter.stream(profile, credential, item.model_request):
                if (
                    event.event_type is ModelStreamEventType.MESSAGE_DELTA
                    and event.message_delta
                    and first_token_ns is None
                ):
                    first_token_ns = time.perf_counter_ns()
                elif event.event_type is ModelStreamEventType.USAGE and event.usage is not None:
                    final_usage = event.usage
                elif (
                    event.event_type is ModelStreamEventType.COMPLETED
                    and event.response is not None
                ):
                    final_response = event.response
                    if final_usage is None:
                        final_usage = event.response.usage
            ended_ns = time.perf_counter_ns()

            if final_response is None or final_usage is None:
                raise RuntimeError(f"M5_RESPONSE_OR_USAGE_MISSING:{attempt_id}")

            actual_cost = (
                Decimal(final_usage.input_tokens) * Decimal("2")
                + Decimal(final_usage.output_tokens) * Decimal("8")
            ) / Decimal(1_000_000)
            result_identity = canonical_json_hash(
                {
                    "version": "m5-cache-online-response-v1",
                    "request_identity": item.request_identity,
                    "provider": M5_PROVIDER,
                    "model": M5_MODEL,
                    "input_tokens": final_usage.input_tokens,
                    "output_tokens": final_usage.output_tokens,
                    "total_tokens": final_usage.total_tokens,
                    "cached_tokens": final_usage.cached_tokens,
                    "cost_cny": str(actual_cost),
                }
            )
            response_receipt = budget.issue_response_receipt(
                reservation_receipt,
                result_identity=result_identity,
                conservative_cost_cny=actual_cost,
            )
            budget.settle(
                attempt_id,
                conservative_cost_cny=actual_cost,
                result_identity=result_identity,
                reservation_receipt=reservation_receipt,
                response_receipt=response_receipt,
            )

            ttft_ms = (
                round((first_token_ns - started_ns) / 1_000_000, 2)
                if first_token_ns is not None
                else None
            )
            e2e_ms = round((ended_ns - started_ns) / 1_000_000, 2)
            answer_ok = all(
                kw.lower() in final_response.content.lower() for kw in item.expected_keywords
            )
            records.append(
                {
                    "request_index": item.request_index,
                    "attempt_id": attempt_id,
                    "phase": item.phase,
                    "prefix_id": item.prefix_id,
                    "prefix_sha256": item.prefix_sha256,
                    "prefix_exact_tokens": item.prefix_exact_tokens,
                    "question_id": item.question_id,
                    "question_text": item.question_text,
                    "local_exact_input_tokens": item.local_exact_input_tokens,
                    "input_tokens": final_usage.input_tokens,
                    "output_tokens": final_usage.output_tokens,
                    "total_tokens": final_usage.total_tokens,
                    "cached_tokens": final_usage.cached_tokens,
                    "ttft_ms": ttft_ms,
                    "e2e_latency_ms": e2e_ms,
                    "finish_reason": final_response.finish_reason,
                    "peak_estimated_cost_cny": str(actual_cost.quantize(Decimal("0.00000001"))),
                    "answer_correct": answer_ok,
                    "response_text": final_response.content,
                }
            )
            if item.request_index < len(planned) and inter_request_pause_seconds > 0:
                await asyncio.sleep(inter_request_pause_seconds)

        attempts = budget.state["attempts"]
        ledger_summary = {
            "scope_id": EXPERIMENT_SCOPE_ID,
            "attempt_count": len(attempts),
            "m5_attempt_count": sum(row.get("stage") == "M5" for row in attempts),
            "reserved_or_unknown_count": sum(
                row.get("status") == "RESERVED_OR_UNKNOWN" for row in attempts
            ),
            "settled_count": sum(row.get("status") == "SETTLED" for row in attempts),
            "charged_or_reserved_cny": str(
                sum((Decimal(str(row["charged_or_reserved_cny"])) for row in attempts), Decimal(0))
            ),
            "round_attempts_used": budget.state["local_experiment_policy"]["round_attempts_used"],
            "round_attempt_limit": budget.state["local_experiment_policy"]["round_attempt_limit"],
            "scope_attempt_limit": budget.state["local_experiment_policy"]["scope_attempt_limit"],
        }

    warm_cached = [
        rec["cached_tokens"] or 0
        for rec in records
        if rec["phase"] in {"warm_same_prefix_1", "warm_same_prefix_2"}
    ]
    control_cached = next(
        (
            rec["cached_tokens"] or 0
            for rec in records
            if rec["phase"] == "control_different_prefix"
        ),
        0,
    )
    total_cost_cny = sum(
        (Decimal(rec["peak_estimated_cost_cny"]) for rec in records),
        Decimal(0),
    )
    evidence = {
        "experiment_id": f"M5-implicit-cache-online-{nonce}",
        "provider": M5_PROVIDER,
        "model": M5_MODEL,
        "base_url": "https://dashscope.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "request_mode": "non_thinking",
        "num_retries": 0,
        "fallback_enabled": False,
        "cache_capability": {
            "configured_endpoint_implicit": capability.implicit.value,
            "configured_endpoint_explicit": capability.explicit.value,
            "direct_dashscope_implicit": direct_capability.implicit.value,
            "direct_dashscope_evidence": direct_capability.evidence,
            "documented_minimum_prefix_tokens": M5_MIN_PREFIX_TOKENS,
        },
        "source_document": str(SOURCE_DOCUMENT_PATH.relative_to(WORKTREE_ROOT)).replace("\\", "/"),
        "requests": records,
        "summary": {
            "total_requests": len(records),
            "all_settled": len(records) == M5_PLANNED_DISPATCHES,
            "all_answers_correct": all(rec["answer_correct"] for rec in records),
            "all_exact_token_counts_matched": all(
                rec["local_exact_input_tokens"] == rec["input_tokens"] for rec in records
            ),
            "first_observation_cached_tokens": records[0]["cached_tokens"],
            "warm_1_cached_tokens": records[1]["cached_tokens"],
            "warm_2_cached_tokens": records[2]["cached_tokens"],
            "control_cached_tokens": records[3]["cached_tokens"],
            "warm_cache_hit_observed": any(tokens > 0 for tokens in warm_cached),
            "control_prefix_isolated": control_cached == 0,
            "total_peak_estimated_cost_cny": str(total_cost_cny.quantize(Decimal("0.00000001"))),
        },
        "ledger": ledger_summary,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "m5-implicit-cache-online-evidence.json"
    evidence_path.write_text(
        json.dumps(copy.deepcopy(evidence), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Run M5 implicit cache online experiment.")
    parser.add_argument("--execute", action="store_true", help="Execute the 4 live dispatches.")
    parser.add_argument(
        "--accept-provider-dispatch",
        action="store_true",
        help="Confirm live provider dispatches to Aliyun Beijing.",
    )
    args = parser.parse_args()
    if not (args.execute and args.accept_provider_dispatch):
        counter = DeepseekV41TextCounter.from_file(DEFAULT_TOKENIZER_PATH)
        source_text = SOURCE_DOCUMENT_PATH.read_text(encoding="utf-8")
        planned = build_planned_requests(
            counter, source_document_text=source_text, run_nonce="dryrun"
        )
        print(
            json.dumps(
                {
                    "mode": "DRY_RUN",
                    "planned_requests": [
                        {
                            "request_index": item.request_index,
                            "phase": item.phase,
                            "prefix_id": item.prefix_id,
                            "prefix_exact_tokens": item.prefix_exact_tokens,
                            "question_id": item.question_id,
                            "local_exact_input_tokens": item.local_exact_input_tokens,
                        }
                        for item in planned
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    result = asyncio.run(execute_m5_cache_online_experiment())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
