"""Single-attempt, budgeted Aliyun calls for the public-memory experiment.

No fallback or retry is performed. Provider failures leave the reservation
unresolved and prevent a later call until the ledger has been reconciled.
"""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget, upper_bound_cost_cny
from packages.agent_runtime.context_budget import ContextAdmissionResult
from packages.model_gateway.contracts import ModelMessage

MODEL = "deepseek-v4.1-flash-1017"
PRICE_IDENTITY = "aliyun-beijing-deepseek-v4.1-flash-1017-peak-2-8-cny-v1"
MAX_RESPONSE_BYTES = 1_048_576


class MemoryTrialDispatchError(Exception):
    """A bounded call failed; details never include credentials or source text."""


async def generate_once(
    *,
    budget: MemoryTrialBudget,
    admission: ContextAdmissionResult,
    original_messages: tuple[ModelMessage, ...],
    attempt_id: str,
    stage: str,
    endpoint: str,
    api_key: str,
    output_limit: int,
    output_directory: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    enable_thinking: bool | None = None,
) -> dict:
    """Send one exactly admitted request after a durable cost reservation."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".cn-beijing.maas.aliyuncs.com")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not api_key
    ):
        raise ValueError("UNVERIFIED_MEMORY_TRIAL_ENDPOINT")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", attempt_id):
        raise ValueError("INVALID_MEMORY_TRIAL_ATTEMPT_ID")
    if budget.state.get("price_identity") != PRICE_IDENTITY:
        raise ValueError("MEMORY_TRIAL_PRICE_IDENTITY_MISMATCH")
    if type(output_limit) is not int or not 0 < output_limit <= 2048:
        raise ValueError("INVALID_MEMORY_TRIAL_OUTPUT_LIMIT")
    if enable_thinking is not None and (
        type(enable_thinking) is not bool or stage != "CALIBRATION"
    ):
        raise ValueError("MEMORY_TRIAL_THINKING_OVERRIDE_NOT_CALIBRATION")
    if (
        admission.messages != original_messages
        or admission.tool_definitions
        or admission.usage.truncated
        or admission.usage.dropped_exchange_count
        or admission.usage.reserved_output < output_limit
        or admission.usage.context_limit != 32768
        or admission.usage.context_policy_version != "adaptive_context_v1"
        or any(
            message.role not in {"system", "user", "assistant"}
            or not isinstance(message.content, str)
            or message.name is not None
            or message.tool_call_id is not None
            or message.tool_calls
            for message in original_messages
        )
    ):
        raise ValueError("MEMORY_TRIAL_REQUEST_NOT_FULLY_ADMITTED")
    wire_messages = [
        {"role": message.role, "content": message.content} for message in original_messages
    ]
    payload = {
        "model": MODEL,
        "messages": wire_messages,
        "max_tokens": output_limit,
        "temperature": 0,
    }
    if enable_thinking is not None:
        payload["enable_thinking"] = enable_thinking
    output_directory.mkdir(parents=True, exist_ok=True)
    result_path = output_directory / f"{attempt_id}.json"
    if result_path.exists():
        raise ValueError("MEMORY_TRIAL_RESULT_ALREADY_EXISTS")
    reserve = upper_bound_cost_cny(
        input_token_bound=32768,
        output_token_bound=output_limit,
        input_per_million=Decimal("2"),
        output_per_million=Decimal("8"),
    )
    budget.reserve(
        attempt_id,
        stage=stage,
        estimated_input_tokens=admission.usage.estimated_input_after,
        requested_output_tokens=output_limit,
        upper_cost_cny=reserve,
    )
    try:
        started = time.perf_counter()
        async with httpx.AsyncClient(
            transport=transport, timeout=60, follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                "POST",
                endpoint.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            ) as response:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ValueError("MEMORY_TRIAL_RESPONSE_TOO_LARGE")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        body = json.loads(chunks)
        usage = body["usage"]
        model = body["model"]
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]
        details = usage.get("prompt_tokens_details")
        cached_tokens = details.get("cached_tokens") if isinstance(details, dict) else None
        output_details = usage.get("completion_tokens_details")
        reasoning_tokens = (
            output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
        )
        choices = body["choices"]
        if (
            model != MODEL
            or type(prompt_tokens) is not int
            or not 0 <= prompt_tokens <= 32768
            or type(completion_tokens) is not int
            or not 0 <= completion_tokens <= output_limit
            or (
                cached_tokens is not None
                and (type(cached_tokens) is not int or not 0 <= cached_tokens <= prompt_tokens)
            )
            or (
                reasoning_tokens is not None
                and (
                    type(reasoning_tokens) is not int
                    or not 0 <= reasoning_tokens <= completion_tokens
                )
            )
            or not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get("message"), dict)
            or not isinstance(choices[0]["message"].get("content"), str)
        ):
            raise ValueError("MEMORY_TRIAL_RESPONSE_UNVERIFIED")
        observed = (Decimal(prompt_tokens) * 2 + Decimal(completion_tokens) * 8) / Decimal(
            1_000_000
        )
        result = {
            "attempt_id": attempt_id,
            "stage": stage,
            "model": model,
            "enable_thinking": enable_thinking,
            "content": choices[0]["message"]["content"],
            "finish_reason": choices[0].get("finish_reason"),
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            # Never persist a provider's reasoning text. Numeric usage is
            # useful to diagnose output-cap exhaustion when it is available.
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": None,
            "elapsed_ms": elapsed_ms,
            "conservative_cost_cny": str(observed),
            "cost_identity": PRICE_IDENTITY,
        }
        temporary = result_path.with_suffix(".json.tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, result_path)
        budget.settle(attempt_id, conservative_cost_cny=observed)
        return result
    except Exception:
        # The ledger remains RESERVED_OR_UNKNOWN, even when an HTTP error
        # cannot prove whether the provider executed the request.
        raise MemoryTrialDispatchError("MEMORY_TRIAL_DISPATCH_UNCONFIRMED") from None
