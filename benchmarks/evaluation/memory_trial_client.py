"""Single-attempt, budgeted Aliyun calls for local memory experiments.

No fallback or retry is performed. The default ledger policy blocks new calls
when an attempt is unresolved. An explicitly injected local experiment budget
adapter may implement a separately approved independent-request strategy; this
client never clears or replays an unknown attempt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from benchmarks.evaluation.memory_deepseek_v41_counter import (
    TOKENIZER_REVISION,
    DeepseekV41TextCounter,
)
from benchmarks.evaluation.memory_trial_budget import (
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
    MemoryTrialBudget,
    upper_bound_cost_cny,
)
from packages.agent_runtime.context_budget import ContextAdmissionResult
from packages.core.canonical.json_hash import canonical_json_hash
from packages.model_gateway.contracts import ModelMessage, ModelRequest

MODEL = "deepseek-v4.1-flash-1017"
PRICE_IDENTITY = "aliyun-beijing-deepseek-v4.1-flash-1017-peak-2-8-cny-v1"
MAX_RESPONSE_BYTES = 1_048_576


class MemoryTrialDispatchError(Exception):
    """Sanitized failure metadata; never retain provider messages or request data."""

    def __init__(self, *, failure_stage: str, cause_type: str, http_status: int | None = None):
        super().__init__("MEMORY_TRIAL_DISPATCH_UNCONFIRMED")
        self.failure_stage = failure_stage
        self.cause_type = cause_type
        self.http_status = http_status


@dataclass(frozen=True)
class NonThinkingExperimentPolicy:
    """Explicit mode binding for the local document B/C experiment only."""

    policy_id: str
    budget_strategy_id: str
    counter_id: str = "deepseek_v41_text_v1"
    thinking_mode: str = "NON_THINKING"
    enable_thinking: bool = False

    def __post_init__(self) -> None:
        if (
            self.policy_id != "memory-bc-nonthinking-v1"
            or self.budget_strategy_id != "RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT"
            or self.counter_id != "deepseek_v41_text_v1"
            or self.thinking_mode != "NON_THINKING"
            or self.enable_thinking is not False
        ):
            raise ValueError("INVALID_MEMORY_TRIAL_EXPERIMENT_POLICY")


class ExperimentBudgetAdapter(Protocol):
    """Adapter implemented with the separately reviewed ledger strategy from A."""

    def reserve_experiment(
        self,
        *,
        strategy_id: str,
        attempt_id: str,
        stage: str,
        estimated_input_tokens: int,
        requested_output_tokens: int,
        upper_cost_cny: Decimal,
        input_counter: dict,
        request_identity: str,
    ) -> None: ...

    def settle_experiment(
        self,
        *,
        strategy_id: str,
        attempt_id: str,
        conservative_cost_cny: Decimal,
        result_identity: str,
    ) -> None: ...


@dataclass(frozen=True)
class TrialReservationReceipt:
    attempt_id: str
    request_identity: str
    scope_id: str
    input_tokens: int
    output_tokens: int
    ledger_reservation: object = field(repr=False, compare=False)
    _capability: object = field(repr=False, compare=False)


class MemoryTrialBudgetExperimentAdapter:
    """Bind NON_THINKING requests and a local input workset to A's schema-v2 scope.

    ``input_limit`` is the experiment's per-request trigger bound. The provider
    admission profile remains separately fixed to the declared 32K ceiling.
    """

    def __init__(
        self,
        budget: MemoryTrialBudget,
        *,
        scope_id: str,
        input_limit: int = 32768,
        provider_context_limit_tokens: int = 32768,
    ):
        if type(input_limit) is not int or not 0 < input_limit <= 32768:
            raise ValueError("MEMORY_TRIAL_EXPERIMENT_INPUT_LIMIT_INVALID")
        if type(provider_context_limit_tokens) is not int or provider_context_limit_tokens != 32768:
            raise ValueError("MEMORY_TRIAL_PROVIDER_CONTEXT_LIMIT_IDENTITY_INVALID")
        self.budget = budget
        self.scope_id = scope_id
        self.input_limit = input_limit
        self.provider_context_limit_tokens = provider_context_limit_tokens
        self._receipt_capability = object()
        self._receipts: dict[str, TrialReservationReceipt] = {}
        self._claimed_attempts: set[str] = set()
        self._observed_responses: dict[str, dict] = {}
        self._lock = RLock()

    def _require_active_scope(self, strategy_id: str) -> None:
        policy = self.budget.state.get("local_experiment_policy")
        if (
            strategy_id != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
            or self.budget.state.get("price_identity") != PRICE_IDENTITY
            or self.budget.state.get("schema_version") != 2
            or not isinstance(policy, dict)
            or policy.get("mode") != RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
            or policy.get("active_scope_id") != self.scope_id
        ):
            raise ValueError("MEMORY_TRIAL_INDEPENDENT_SCOPE_NOT_ACTIVE")

    def validate_spec(self, spec) -> None:
        policy = self.budget.state.get("local_experiment_policy")
        strategy_id = policy.get("mode") if isinstance(policy, dict) else ""
        self._require_active_scope(strategy_id)
        if (
            spec.price.identity != PRICE_IDENTITY
            or spec.model_id != MODEL
            or spec.route_id != "aliyun-beijing"
            or spec.thinking is not False
            or spec.provider_context_limit_tokens != self.provider_context_limit_tokens
            or spec.workset_tokens != self.input_limit
            or any(cap > 2048 for cap in spec.output_caps.values())
        ):
            raise ValueError("MEMORY_TRIAL_LOCAL_SPEC_IDENTITY_MISMATCH")

    def _new_receipt(
        self,
        *,
        strategy_id: str,
        attempt_id: str,
        attempt_kind: str,
        output_tokens: int,
        upper_cost_cny: Decimal,
        input_counter: dict,
        request_identity: str,
    ) -> TrialReservationReceipt:
        self._require_active_scope(strategy_id)
        if (
            input_counter.get("thinking_mode") != "NON_THINKING"
            or input_counter.get("request_identity") != request_identity
            or type(input_counter.get("tokens")) is not int
            or type(input_counter.get("input_limit")) is not int
            or input_counter["input_limit"] != self.input_limit
        ):
            raise ValueError("MEMORY_TRIAL_NON_THINKING_RESERVATION_IDENTITY_INVALID")
        inner = self.budget.reserve(
            attempt_id,
            stage="M4",
            estimated_input_tokens=self.provider_context_limit_tokens,
            requested_output_tokens=output_tokens,
            upper_cost_cny=upper_cost_cny,
            input_counter=input_counter,
            request_identity=request_identity,
        )
        receipt = TrialReservationReceipt(
            attempt_id=attempt_id,
            request_identity=request_identity,
            scope_id=self.scope_id,
            input_tokens=input_counter["tokens"],
            output_tokens=output_tokens,
            ledger_reservation=inner,
            _capability=self._receipt_capability,
        )
        with self._lock:
            if attempt_id in self._receipts:
                raise ValueError("MEMORY_TRIAL_RESERVATION_RECEIPT_DUPLICATE")
            self._receipts[attempt_id] = receipt
        return receipt

    def reserve_experiment(
        self,
        *,
        strategy_id: str,
        attempt_id: str,
        stage: str,
        estimated_input_tokens: int,
        requested_output_tokens: int,
        upper_cost_cny: Decimal,
        input_counter: dict,
        request_identity: str,
    ) -> TrialReservationReceipt:
        if estimated_input_tokens != self.provider_context_limit_tokens or stage != "M4":
            raise ValueError("MEMORY_TRIAL_FULL_WINDOW_RESERVATION_REQUIRED")
        return self._new_receipt(
            strategy_id=strategy_id,
            attempt_id=attempt_id,
            attempt_kind=stage,
            output_tokens=requested_output_tokens,
            upper_cost_cny=upper_cost_cny,
            input_counter=input_counter,
            request_identity=request_identity,
        )

    def reserve_attempt(
        self,
        *,
        attempt_id: str,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        upper_cost_cny: Decimal,
        request_identity: str,
    ) -> TrialReservationReceipt:
        """Runner adapter: reserve first and bind the exact NON_THINKING request."""
        policy = self.budget.state.get("local_experiment_policy")
        strategy_id = policy.get("mode") if isinstance(policy, dict) else ""
        if (
            stage not in {"summary", "B", "C"}
            or type(input_tokens) is not int
            or not 0 < input_tokens <= self.input_limit
        ):
            raise ValueError("MEMORY_TRIAL_RUNNER_RESERVATION_INVALID")
        input_counter = {
            "counter_id": "deepseek_v41_text_v1",
            "tokenizer_revision": TOKENIZER_REVISION,
            "thinking_mode": "NON_THINKING",
            "tokens": input_tokens,
            "input_limit": self.input_limit,
            "request_identity": request_identity,
        }
        return self._new_receipt(
            strategy_id=strategy_id,
            attempt_id=attempt_id,
            attempt_kind=stage,
            output_tokens=output_tokens,
            upper_cost_cny=upper_cost_cny,
            input_counter=input_counter,
            request_identity=request_identity,
        )

    def _validate_receipt(
        self,
        receipt: object,
        *,
        attempt_id: str,
        request_identity: str,
        output_tokens: int,
    ) -> dict:
        if (
            not isinstance(receipt, TrialReservationReceipt)
            or receipt._capability is not self._receipt_capability
            or self._receipts.get(attempt_id) is not receipt
            or receipt.attempt_id != attempt_id
            or receipt.scope_id != self.scope_id
            or receipt.request_identity != request_identity
            or receipt.output_tokens != output_tokens
        ):
            raise ValueError("MEMORY_TRIAL_RESERVATION_RECEIPT_INVALID")
        policy = self.budget.state.get("local_experiment_policy")
        strategy_id = policy.get("mode") if isinstance(policy, dict) else ""
        self._require_active_scope(strategy_id)
        row = next(
            (
                item
                for item in self.budget.state.get("attempts", [])
                if item.get("attempt_id") == attempt_id
            ),
            None,
        )
        counter = row.get("input_counter") if isinstance(row, dict) else None
        if (
            row is None
            or row.get("status") != "RESERVED_OR_UNKNOWN"
            or row.get("scope_id") != self.scope_id
            or row.get("request_identity") != request_identity
            or row.get("requested_output_tokens") != output_tokens
            or not isinstance(counter, dict)
            or counter.get("thinking_mode") != "NON_THINKING"
            or counter.get("request_identity") != request_identity
            or counter.get("tokens") != receipt.input_tokens
        ):
            raise ValueError("MEMORY_TRIAL_LEDGER_RESERVATION_MISMATCH")
        return row

    def claim_dispatch(
        self,
        receipt: object,
        *,
        attempt_id: str,
        request_identity: str,
        output_tokens: int,
    ) -> None:
        with self._lock:
            self._validate_receipt(
                receipt,
                attempt_id=attempt_id,
                request_identity=request_identity,
                output_tokens=output_tokens,
            )
            if attempt_id in self._claimed_attempts:
                raise ValueError("MEMORY_TRIAL_RESERVATION_ALREADY_DISPATCHED")
            self._claimed_attempts.add(attempt_id)

    def observe_provider_response(
        self,
        receipt: object,
        *,
        attempt_id: str,
        request_identity: str,
        output_tokens: int,
        content: str,
        provider: str,
        model: str,
        input_tokens: int,
        response_tokens: int,
        total_tokens: int,
    ) -> object:
        from packages.agent_runtime.frozen import (
            SUMMARY_NON_THINKING_MODEL,
            SUMMARY_NON_THINKING_PROVIDER,
        )

        with self._lock:
            self._validate_receipt(
                receipt,
                attempt_id=attempt_id,
                request_identity=request_identity,
                output_tokens=output_tokens,
            )
            if (
                attempt_id not in self._claimed_attempts
                or attempt_id in self._observed_responses
                or provider != SUMMARY_NON_THINKING_PROVIDER
                or model != SUMMARY_NON_THINKING_MODEL
                or not isinstance(content, str)
                or type(input_tokens) is not int
                or type(response_tokens) is not int
                or total_tokens != input_tokens + response_tokens
                or response_tokens > output_tokens
                or input_tokens > self.provider_context_limit_tokens
            ):
                raise ValueError("MEMORY_TRIAL_PROVIDER_RESPONSE_PROOF_INVALID")
            response_identity = hashlib.sha256(content.encode("utf-8")).hexdigest()
            actual_cost = (
                Decimal(input_tokens) * Decimal("2")
                + Decimal(response_tokens) * Decimal("8")
            ) / Decimal(1_000_000)
            proof = self.budget.issue_response_receipt(
                receipt.ledger_reservation,
                result_identity=response_identity,
                conservative_cost_cny=actual_cost,
            )
            self._observed_responses[attempt_id] = {
                "proof": proof,
                "response_identity": response_identity,
                "input_tokens": input_tokens,
                "output_tokens": response_tokens,
                "provider": provider,
                "model": model,
            }
            return proof

    def settle_attempt(self, *, reservation: object, reply) -> None:
        attempt_id = getattr(reservation, "attempt_id", None)
        request_identity = getattr(reservation, "request_identity", None)
        output_tokens = getattr(reservation, "output_tokens", None)
        with self._lock:
            self._validate_receipt(
                reservation,
                attempt_id=attempt_id,
                request_identity=request_identity,
                output_tokens=output_tokens,
            )
            observed = self._observed_responses.get(attempt_id)
            expected_cost = (
                Decimal(observed["input_tokens"]) * Decimal("2")
                + Decimal(observed["output_tokens"]) * Decimal("8")
            ) / Decimal(1_000_000) if observed is not None else None
            if (
                observed is None
                or getattr(reply, "dispatch_proof", None) is not observed["proof"]
                or getattr(reply, "provider_response_identity", None)
                != observed["response_identity"]
                or getattr(reply, "input_tokens", None) != observed["input_tokens"]
                or getattr(reply, "output_tokens", None) != observed["output_tokens"]
                or getattr(reply, "model", None) != observed["model"]
                or getattr(reply, "actual_cost_cny", None) != expected_cost
                or hashlib.sha256(getattr(reply, "content", "").encode("utf-8")).hexdigest()
                != observed["response_identity"]
            ):
                raise ValueError("MEMORY_TRIAL_RESPONSE_SETTLEMENT_PROOF_INVALID")
            actual_cost = (
                Decimal(observed["input_tokens"]) * Decimal("2")
                + Decimal(observed["output_tokens"]) * Decimal("8")
            ) / Decimal(1_000_000)
            self.budget.settle(
                attempt_id,
                conservative_cost_cny=actual_cost,
                result_identity=observed["response_identity"],
                reservation_receipt=reservation.ledger_reservation,
                response_receipt=observed["proof"],
            )
            row = next(
                item
                for item in self.budget.state["attempts"]
                if item.get("attempt_id") == attempt_id
            )
            if row.get("status") != "SETTLED":
                raise ValueError("MEMORY_TRIAL_SETTLEMENT_NOT_CONFIRMED")
            self._observed_responses.pop(attempt_id, None)

    def settle_experiment(
        self,
        *,
        reservation: TrialReservationReceipt,
        result: dict,
        dispatch_proof: object,
    ) -> None:
        reply = SimpleNamespace(
            content=result["content"],
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
            model=result["model"],
            dispatch_proof=dispatch_proof,
            provider_response_identity=hashlib.sha256(
                result["content"].encode("utf-8")
            ).hexdigest(),
        )
        self.settle_attempt(reservation=reservation, reply=reply)


def _request_payload(
    messages: tuple[ModelMessage, ...], output_limit: int, enable_thinking: bool | None
) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": message.role, "content": message.content} for message in messages],
        "max_tokens": output_limit,
        "temperature": 0,
    }
    if enable_thinking is not None:
        payload["enable_thinking"] = enable_thinking
    return payload


def trial_request_identity(
    *,
    endpoint: str,
    messages: tuple[ModelMessage, ...],
    output_limit: int,
    enable_thinking: bool | None = None,
) -> str:
    """Bind a trial attempt to its full endpoint and provider-visible request."""
    return canonical_json_hash(
        {
            "version": "memory-trial-request-v1",
            "endpoint": endpoint.rstrip("/") + "/chat/completions",
            "price_identity": PRICE_IDENTITY,
            "payload": _request_payload(messages, output_limit, enable_thinking),
        }
    )


def trial_result_identity(result: dict) -> str:
    """Bind a saved result to its reservation without storing content in the ledger."""
    return canonical_json_hash({"version": "memory-trial-result-v1", "result": result})


class AliyunM4BCDispatcher:
    """One reserved NON_THINKING request; intended for injection into the B/C runner.

    The runner reserves through ``MemoryTrialBudgetExperimentAdapter`` first and
    passes its receipt here. This dispatcher never retries and leaves settlement
    to the runner after it durably records the response.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        reservation_authority: MemoryTrialBudgetExperimentAdapter | None = None,
    ):
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
        self.endpoint = endpoint
        self.api_key = api_key
        self.transport = transport
        self.reservation_authority = reservation_authority

    def bind_reservation_authority(self, authority) -> None:
        if self.reservation_authority is not None and self.reservation_authority is not authority:
            raise ValueError("MEMORY_TRIAL_RESERVATION_AUTHORITY_ALREADY_BOUND")
        if not callable(getattr(authority, "claim_dispatch", None)) or not callable(
            getattr(authority, "observe_provider_response", None)
        ):
            raise ValueError("MEMORY_TRIAL_RESERVATION_AUTHORITY_INVALID")
        self.reservation_authority = authority

    def validate_spec(self, spec) -> None:
        if (
            spec.price.identity != PRICE_IDENTITY
            or spec.model_id != MODEL
            or spec.route_id != "aliyun-beijing"
            or spec.thinking is not False
            or spec.provider_context_limit_tokens != 32768
            or any(cap > 2048 for cap in spec.output_caps.values())
        ):
            raise ValueError("MEMORY_TRIAL_PROVIDER_SPEC_IDENTITY_MISMATCH")

    @staticmethod
    def _messages(request) -> tuple[ModelMessage, ...]:
        return tuple(
            ModelMessage(role=item.role, content=item.content) for item in request.messages
        )

    def request_identity(self, request) -> str:
        return trial_request_identity(
            endpoint=self.endpoint,
            messages=self._messages(request),
            output_limit=request.max_output_tokens,
            enable_thinking=False,
        )

    def dispatch(self, request, *, reservation: TrialReservationReceipt) -> dict:
        if (
            not isinstance(reservation, TrialReservationReceipt)
            or reservation.attempt_id != request.attempt_id
            or reservation.request_identity != self.request_identity(request)
            or reservation.scope_id == ""
            or reservation.output_tokens != request.max_output_tokens
            or request.stage not in {"summary", "B", "C"}
        ):
            raise ValueError("MEMORY_TRIAL_DISPATCH_RESERVATION_MISMATCH")
        authority = self.reservation_authority
        if authority is None:
            raise ValueError("MEMORY_TRIAL_RESERVATION_AUTHORITY_REQUIRED")
        authority.claim_dispatch(
            reservation,
            attempt_id=request.attempt_id,
            request_identity=self.request_identity(request),
            output_tokens=request.max_output_tokens,
        )
        payload = _request_payload(
            self._messages(request), request.max_output_tokens, enable_thinking=False
        )
        started = time.perf_counter()
        failure_stage = "HTTP_TRANSPORT"
        http_status = None
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=60,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    "POST",
                    self.endpoint.rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as response:
                    http_status = response.status_code
                    response.raise_for_status()
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_RESPONSE_BYTES:
                            raise ValueError("MEMORY_TRIAL_RESPONSE_TOO_LARGE")
            failure_stage = "RESPONSE_DECODE"
            body = json.loads(chunks)
            usage = body["usage"]
            prompt_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
            details = usage.get("prompt_tokens_details")
            cached_tokens = details.get("cached_tokens") if isinstance(details, dict) else None
            output_details = usage.get("completion_tokens_details")
            reasoning_tokens = (
                output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
            )
            choices = body["choices"]
            failure_stage = "RESPONSE_VALIDATION"
            if (
                body.get("model") != MODEL
                or type(prompt_tokens) is not int
                or not 0 <= prompt_tokens <= 32768
                or type(output_tokens) is not int
                or not 0 <= output_tokens <= request.max_output_tokens
                or (cached_tokens is not None and type(cached_tokens) is not int)
                or (reasoning_tokens is not None and type(reasoning_tokens) is not int)
                or not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get("message"), dict)
                or not isinstance(choices[0]["message"].get("content"), str)
                or not choices[0].get("finish_reason")
            ):
                raise ValueError("MEMORY_TRIAL_RESPONSE_UNVERIFIED")
            cost = (Decimal(prompt_tokens) * 2 + Decimal(output_tokens) * 8) / Decimal(1_000_000)
            result = {
                "content": choices[0]["message"]["content"],
                "input_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "actual_cost_cny": cost,
                "cost_identity": PRICE_IDENTITY,
                "finish_reason": choices[0]["finish_reason"],
                "cached_tokens": cached_tokens,
                "reasoning_tokens": reasoning_tokens,
                "provider_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "model": body["model"],
            }
            failure_stage = "RESPONSE_RECEIPT"
            result["dispatch_proof"] = authority.observe_provider_response(
                reservation,
                attempt_id=request.attempt_id,
                request_identity=self.request_identity(request),
                output_tokens=request.max_output_tokens,
                content=result["content"],
                provider="openai-compatible",
                model=result["model"],
                input_tokens=prompt_tokens,
                response_tokens=output_tokens,
                total_tokens=prompt_tokens + output_tokens,
            )
            result["provider_response_identity"] = hashlib.sha256(
                result["content"].encode("utf-8")
            ).hexdigest()
            return result
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                failure_stage = "HTTP_STATUS"
                http_status = exc.response.status_code
            elif isinstance(exc, httpx.ConnectTimeout):
                failure_stage = "HTTP_CONNECT_TIMEOUT"
            elif isinstance(exc, httpx.ConnectError):
                failure_stage = "HTTP_CONNECT"
            elif isinstance(exc, httpx.ReadTimeout):
                failure_stage = "HTTP_READ_TIMEOUT"
            elif isinstance(exc, httpx.RemoteProtocolError):
                failure_stage = "HTTP_PROTOCOL"
            elif isinstance(exc, json.JSONDecodeError):
                failure_stage = "RESPONSE_JSON"
            safe_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:64] or "Exception"
            raise MemoryTrialDispatchError(
                failure_stage=failure_stage,
                cause_type=safe_type,
                http_status=http_status,
            ) from None


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
    provider_counter: DeepseekV41TextCounter | None = None,
    provider_input_limit: int | None = None,
    experiment_policy: NonThinkingExperimentPolicy | None = None,
    experiment_budget: ExperimentBudgetAdapter | None = None,
    experiment_input_limit: int | None = None,
) -> dict:
    """Send one request after durable reservation and exact message counting.

    The 32K value in ``admission`` is the provider-declared admission ceiling. In explicit
    NON_THINKING experiments, ``experiment_input_limit`` is a smaller local
    workset ceiling and does not redefine the provider's model window.
    """
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
    if experiment_policy is not None:
        if (
            stage != "M4"
            or enable_thinking is not None
            or not isinstance(experiment_policy, NonThinkingExperimentPolicy)
            or not callable(getattr(experiment_budget, "reserve_experiment", None))
            or not callable(getattr(experiment_budget, "settle_experiment", None))
            or type(experiment_input_limit) is not int
            or experiment_input_limit <= 0
            or experiment_input_limit > admission.usage.context_limit
        ):
            raise ValueError("MEMORY_TRIAL_EXPERIMENT_POLICY_OR_BUDGET_ADAPTER_INVALID")
    elif experiment_input_limit is not None:
        raise ValueError("MEMORY_TRIAL_EXPERIMENT_INPUT_LIMIT_REQUIRES_POLICY")
    effective_thinking = (
        experiment_policy.enable_thinking if experiment_policy is not None else enable_thinking
    )
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
    input_counter = None
    if stage == "M4":
        if (
            not isinstance(provider_counter, DeepseekV41TextCounter)
            or not provider_counter.verified_source
        ):
            raise ValueError("MEMORY_TRIAL_PROVIDER_COUNTER_REQUIRED")
        if experiment_policy is not None:
            provider_input_limit = experiment_input_limit
        elif provider_input_limit is None:
            provider_input_limit = admission.usage.available_input
        if (
            type(provider_input_limit) is not int
            or not 0 < provider_input_limit <= admission.usage.available_input
        ):
            raise ValueError("MEMORY_TRIAL_PROVIDER_INPUT_LIMIT_INVALID")
        counter_thinking = experiment_policy.enable_thinking if experiment_policy else True
        actual_estimate = provider_counter.count(
            ModelRequest(original_messages), thinking=counter_thinking
        )
        if actual_estimate > provider_input_limit:
            raise ValueError("MEMORY_TRIAL_PROVIDER_WINDOW_EXCEEDED")
        input_counter = {
            "counter_id": experiment_policy.counter_id
            if experiment_policy
            else "deepseek_v41_text_v1",
            "tokenizer_revision": TOKENIZER_REVISION,
            "thinking_mode": (
                experiment_policy.thinking_mode
                if experiment_policy
                else "PROVIDER_DEFAULT_THINKING"
            ),
            "tokens": actual_estimate,
            "input_limit": provider_input_limit,
        }
    payload = _request_payload(original_messages, output_limit, effective_thinking)
    request_identity = trial_request_identity(
        endpoint=endpoint,
        messages=original_messages,
        output_limit=output_limit,
        enable_thinking=effective_thinking,
    )
    if experiment_policy is not None:
        input_counter["request_identity"] = request_identity
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
    if experiment_policy is None:
        budget.reserve(
            attempt_id,
            stage=stage,
            # The shared attempt cap must not inherit a known undercount from the
            # approximate policy. Charge the declared full window to the cap.
            estimated_input_tokens=32768,
            requested_output_tokens=output_limit,
            upper_cost_cny=reserve,
            input_counter=input_counter,
            request_identity=request_identity,
        )
    else:
        experiment_budget.reserve_experiment(
            strategy_id=experiment_policy.budget_strategy_id,
            attempt_id=attempt_id,
            stage=stage,
            estimated_input_tokens=32768,
            requested_output_tokens=output_limit,
            upper_cost_cny=reserve,
            input_counter=input_counter,
            request_identity=request_identity,
        )
    failure_stage = "HTTP_TRANSPORT"
    http_status = None
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
                http_status = response.status_code
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ValueError("MEMORY_TRIAL_RESPONSE_TOO_LARGE")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        failure_stage = "RESPONSE_DECODE"
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
        response_input_limit = provider_input_limit if stage == "M4" else 32768
        failure_stage = "RESPONSE_VALIDATION"
        if (
            model != MODEL
            or type(prompt_tokens) is not int
            or not 0 <= prompt_tokens <= response_input_limit
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
            or (
                stage == "M4"
                and input_counter is not None
                and prompt_tokens != input_counter["tokens"]
            )
        ):
            raise ValueError("MEMORY_TRIAL_RESPONSE_UNVERIFIED")
        observed = (Decimal(prompt_tokens) * 2 + Decimal(completion_tokens) * 8) / Decimal(
            1_000_000
        )
        result = {
            "attempt_id": attempt_id,
            "stage": stage,
            "model": model,
            "enable_thinking": effective_thinking,
            "input_counter": input_counter,
            "request_identity": request_identity,
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
        if experiment_policy is None:
            budget.settle(
                attempt_id,
                conservative_cost_cny=observed,
                result_identity=trial_result_identity(result),
            )
        else:
            experiment_budget.settle_experiment(
                strategy_id=experiment_policy.budget_strategy_id,
                attempt_id=attempt_id,
                conservative_cost_cny=observed,
                result_identity=trial_result_identity(result),
            )
        return result
    except Exception as exc:
        # The ledger remains RESERVED_OR_UNKNOWN, even when an HTTP error
        # cannot prove whether the provider executed the request.
        if isinstance(exc, httpx.HTTPStatusError):
            failure_stage = "HTTP_STATUS"
            http_status = exc.response.status_code
        elif isinstance(exc, httpx.ConnectTimeout):
            failure_stage = "HTTP_CONNECT_TIMEOUT"
        elif isinstance(exc, httpx.ConnectError):
            failure_stage = "HTTP_CONNECT"
        elif isinstance(exc, httpx.ReadTimeout):
            failure_stage = "HTTP_READ_TIMEOUT"
        elif isinstance(exc, httpx.RemoteProtocolError):
            failure_stage = "HTTP_PROTOCOL"
        elif isinstance(exc, json.JSONDecodeError):
            failure_stage = "RESPONSE_JSON"
        safe_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:64] or "Exception"
        raise MemoryTrialDispatchError(
            failure_stage=failure_stage,
            cause_type=safe_type,
            http_status=http_status,
        ) from None
