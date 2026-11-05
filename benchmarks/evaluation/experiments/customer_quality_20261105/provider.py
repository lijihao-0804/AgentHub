"""Budget-enforced, single-attempt Aliyun provider client for T04."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Final
from urllib.parse import quote, urlsplit

from benchmarks.evaluation.experiments.customer_quality_20261105.budget import (
    BudgetHalted,
    CallStateConflict,
    ResearchBudgetLedger,
    Reservation,
)

MODEL: Final = "deepseek-v4.1-flash-1017"
_RUNTIME_MAX_TOKENS: Final = 2_048
_ALLOWED_JUDGE_MAX_TOKENS: Final = frozenset({2_048, 8_192})
_ALIYUN_BEIJING_HOST_SUFFIX: Final = ".cn-beijing.maas.aliyuncs.com"
_ALIYUN_BEIJING_OPENAI_PATH: Final = "/compatible-mode/v1"
_MISSING = object()
_REQUEST_EVIDENCE_KEYS: Final = (
    "model",
    "messages",
    "tools",
    "response_format",
    "max_tokens",
    "temperature",
    "timeout",
    "num_retries",
    "stream",
)
_FORBIDDEN_EVIDENCE_KEY_MARKERS: Final = (
    "credential",
    "api_key",
    "apikey",
    "authorization",
    "base_url",
    "baseurl",
    "secret",
    "password",
)

type MetadataValue = str | int | bool | None
type Completion = Callable[..., Awaitable[Any]]


class BudgetedProviderError(RuntimeError):
    """A fixed, non-sensitive provider failure suitable for callers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _ScopeState:
    base_key: str
    metadata: Mapping[str, MetadataValue]
    next_ordinal: int = 1


class _InvalidOutcome(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


async def _single_http_completion(**kwargs: Any) -> dict[str, Any]:
    """One physical request, with no SDK message-repair retry or redirects."""
    import httpx

    allowed = {
        "model",
        "messages",
        "temperature",
        "max_tokens",
        "stream",
        "tools",
        "response_format",
        "tool_choice",
        "parallel_tool_calls",
        "seed",
    }
    payload = {key: value for key, value in kwargs.items() if key in allowed}
    payload["model"] = payload["model"].removeprefix("openai/")
    async with httpx.AsyncClient(
        timeout=kwargs["timeout"],
        follow_redirects=False,
        transport=httpx.AsyncHTTPTransport(retries=0),
    ) as client:
        response = await client.post(
            kwargs["api_base"].rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + kwargs["api_key"]},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


class BudgetedAliyunClient:
    """LiteLLM-compatible client with a durable budget and dispatch boundary.

    Usage::

        meter = BudgetedAliyunClient(ledger, base_url, private_dir)
        adapter = LiteLLMProviderAdapter(client=meter)
        with meter.scope("customer-quality/<case_id>/<judge_id>", metadata):
            response = await adapter.complete(profile, credential, request)

    Each provider call in a scope receives the stable logical key
    ``<scope key>/<ordinal>``, starting at ordinal 1.
    """

    def __init__(
        self,
        ledger: ResearchBudgetLedger,
        base_url: str,
        private_dir: str | Path,
        model: str = MODEL,
        client: Completion | None = None,
        judge_max_tokens: int = _RUNTIME_MAX_TOKENS,
    ) -> None:
        if not _is_verified_aliyun_beijing_endpoint(base_url):
            raise ValueError(
                "base_url must be the verified Aliyun Beijing OpenAI-compatible endpoint"
            )
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        if type(judge_max_tokens) is not int or judge_max_tokens not in _ALLOWED_JUDGE_MAX_TOKENS:
            raise ValueError("judge_max_tokens must be 2048 or 8192")

        self.ledger = ledger
        self.model = model
        self._judge_max_tokens = judge_max_tokens
        self.private_dir = Path(private_dir).expanduser().resolve()
        self.private_dir.mkdir(parents=True, exist_ok=True)
        self._base_url = base_url
        self._scope: ContextVar[_ScopeState | None] = ContextVar(
            f"customer-quality-provider-scope-{id(self)}",
            default=None,
        )
        self._records: list[dict[str, Any]] = []
        if client is None:
            self._completion = _single_http_completion
        else:
            self._completion = client

    @contextmanager
    def scope(
        self,
        key: str,
        metadata: Mapping[str, MetadataValue],
    ) -> Iterator[None]:
        """Bind calls in this synchronous context to one stable logical-call scope."""

        if not isinstance(key, str) or not key:
            raise BudgetedProviderError("INVALID_SCOPE_KEY")
        if not isinstance(metadata, Mapping):
            raise BudgetedProviderError("INVALID_SCOPE_METADATA")
        state = _ScopeState(base_key=key, metadata=dict(metadata))
        token = self._scope.set(state)
        try:
            yield
        finally:
            self._scope.reset(token)

    @property
    def records(self) -> list[dict[str, Any]]:
        """Return whitelist-only physical-attempt records for case-level aggregation."""

        return deepcopy(self._records)

    async def acompletion(self, **kwargs: Any) -> Any:
        """Perform exactly one validated, reserved, and recorded provider call."""

        scope = self._scope.get()
        if scope is None:
            raise BudgetedProviderError("PROVIDER_SCOPE_REQUIRED")
        if bool(self.ledger.summary()["halted"]):
            raise BudgetHalted("BUDGET_HALTED", "Research ledger is already halted")

        self._validate_request(kwargs, scope=scope)
        ordinal = scope.next_ordinal
        scope.next_ordinal += 1
        logical_call_key = f"{scope.base_key}/{ordinal}"

        reservation = self.ledger.reserve(logical_call_key, scope.metadata)
        if not reservation.newly_reserved:
            raise CallStateConflict(
                "LOGICAL_CALL_ALREADY_EXISTS",
                f"Logical call {logical_call_key!r} already exists",
            )

        self.ledger.mark_dispatched(logical_call_key)
        started = perf_counter()
        try:
            response = await self._completion(**kwargs)
        except asyncio.CancelledError:
            unknown = self.ledger.mark_unknown(
                logical_call_key,
                "PROVIDER_DISPATCH_CANCELLED",
            )
            self._append_record(
                scope=scope,
                reservation=unknown,
                ordinal=ordinal,
                usage=None,
                latency_ms=(perf_counter() - started) * 1_000,
            )
            raise
        except Exception:
            unknown = self.ledger.mark_unknown(
                logical_call_key,
                "PROVIDER_DISPATCH_UNCONFIRMED",
            )
            self._append_record(
                scope=scope,
                reservation=unknown,
                ordinal=ordinal,
                usage=None,
                latency_ms=(perf_counter() - started) * 1_000,
            )
            raise BudgetedProviderError("PROVIDER_DISPATCH_UNCONFIRMED") from None
        latency_ms = (perf_counter() - started) * 1_000

        try:
            prompt_tokens, completion_tokens = self._validate_response(response)
        except _InvalidOutcome as error:
            unknown = self.ledger.mark_unknown(logical_call_key, error.reason)
            self._append_record(
                scope=scope,
                reservation=unknown,
                ordinal=ordinal,
                usage=None,
                latency_ms=latency_ms,
            )
            raise BudgetedProviderError(error.code) from None
        except Exception:
            unknown = self.ledger.mark_unknown(
                logical_call_key,
                "PROVIDER_RESPONSE_INVALID",
            )
            self._append_record(
                scope=scope,
                reservation=unknown,
                ordinal=ordinal,
                usage=None,
                latency_ms=latency_ms,
            )
            raise BudgetedProviderError("PROVIDER_RESPONSE_INVALID") from None

        try:
            settled = self.ledger.settle(logical_call_key, prompt_tokens, completion_tokens)
        except Exception:
            unknown = self.ledger.mark_unknown(
                logical_call_key,
                "PROVIDER_SETTLEMENT_FAILED",
            )
            self._append_record(
                scope=scope,
                reservation=unknown,
                ordinal=ordinal,
                usage=None,
                latency_ms=latency_ms,
            )
            raise BudgetedProviderError("PROVIDER_SETTLEMENT_FAILED") from None

        self._append_record(
            scope=scope,
            reservation=settled,
            ordinal=ordinal,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            latency_ms=latency_ms,
        )
        try:
            self._write_evidence(
                logical_call_key=logical_call_key,
                ordinal=ordinal,
                kwargs=kwargs,
                response=response,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
        except Exception:
            self.ledger.halt("PROVIDER_EVIDENCE_WRITE_FAILED")
            raise BudgetedProviderError("PROVIDER_EVIDENCE_WRITE_FAILED") from None
        return response

    def _append_record(
        self,
        *,
        scope: _ScopeState,
        reservation: Reservation,
        ordinal: int,
        usage: dict[str, int] | None,
        latency_ms: float,
    ) -> None:
        self._records.append(
            {
                "logical_call_key": f"{scope.base_key}/{ordinal}",
                "metadata": dict(scope.metadata),
                "usage": usage,
                "actual_cny": reservation.actual_cny,
                "estimated_cny": reservation.reserved_cny,
                "status": reservation.status,
                "latency_ms": latency_ms,
            }
        )

    def _validate_request(
        self,
        kwargs: Mapping[str, Any],
        *,
        scope: _ScopeState,
    ) -> None:
        if kwargs.get("extra_body"):
            raise BudgetedProviderError("UNFROZEN_REQUEST_EXTENSION")
        model = kwargs.get("model")
        if model not in {self.model, f"openai/{self.model}"}:
            raise BudgetedProviderError("INVALID_PROVIDER_MODEL")
        if kwargs.get("api_base") != self._base_url:
            raise BudgetedProviderError("INVALID_PROVIDER_ENDPOINT")
        if type(kwargs.get("num_retries")) is not int or kwargs.get("num_retries") != 0:
            raise BudgetedProviderError("INVALID_RETRY_POLICY")
        if kwargs.get("stream") is not False:
            raise BudgetedProviderError("STREAMING_NOT_ALLOWED")
        is_judge_scope = scope.base_key.startswith("calibration/") or scope.base_key.endswith(
            "/judge"
        )
        max_tokens_cap = self._judge_max_tokens if is_judge_scope else _RUNTIME_MAX_TOKENS
        if type(kwargs.get("max_tokens")) is not int or kwargs.get("max_tokens") != max_tokens_cap:
            raise BudgetedProviderError("INVALID_MAX_TOKENS")
        temperature = kwargs.get("temperature")
        if isinstance(temperature, bool) or temperature != 0:
            raise BudgetedProviderError("INVALID_TEMPERATURE")

    def _validate_response(self, response: Any) -> tuple[int, int]:
        if _value(response, "model") != self.model:
            raise _InvalidOutcome(
                "PROVIDER_MODEL_IDENTITY_DRIFT",
                "PROVIDER_MODEL_IDENTITY_DRIFT",
            )

        usage = _value(response, "usage", _MISSING)
        if usage is _MISSING or usage is None:
            raise _InvalidOutcome("PROVIDER_USAGE_MISSING", "PROVIDER_USAGE_MISSING")
        prompt_tokens = _value(usage, "prompt_tokens", _MISSING)
        completion_tokens = _value(usage, "completion_tokens", _MISSING)
        total_tokens = _value(usage, "total_tokens", _MISSING)
        values = (prompt_tokens, completion_tokens, total_tokens)
        if any(type(value) is not int for value in values):
            raise _InvalidOutcome("PROVIDER_USAGE_INVALID", "PROVIDER_USAGE_INVALID")
        if any(value < 0 for value in values):
            raise _InvalidOutcome("PROVIDER_USAGE_INVALID", "PROVIDER_USAGE_INVALID")
        if total_tokens != prompt_tokens + completion_tokens:
            raise _InvalidOutcome("PROVIDER_USAGE_TOTAL_MISMATCH", "PROVIDER_USAGE_TOTAL_MISMATCH")
        return prompt_tokens, completion_tokens

    def _write_evidence(
        self,
        *,
        logical_call_key: str,
        ordinal: int,
        kwargs: Mapping[str, Any],
        response: Any,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
    ) -> None:
        choices = _value(response, "choices", []) or []
        choice = choices[0] if choices else {}
        message = _value(choice, "message", {})
        payload = {
            "stable_key": logical_call_key,
            "ordinal": ordinal,
            "request": {key: kwargs[key] for key in _REQUEST_EVIDENCE_KEYS if key in kwargs},
            "response": {
                "model": _value(response, "model"),
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "content": _value(message, "content", "") or "",
                "tool_calls": [
                    _tool_call_evidence(tool_call)
                    for tool_call in (_value(message, "tool_calls", []) or [])
                ],
                "finish_reason": _value(choice, "finish_reason"),
            },
            "latency_ms": latency_ms,
        }
        encoded = json.dumps(
            _safe_json(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.private_dir.mkdir(parents=True, exist_ok=True)
        destination = self.private_dir / (quote(logical_call_key, safe="") + ".json")
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.private_dir,
                prefix=f".{destination.stem}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
        finally:
            if temporary_path is not None:
                Path(temporary_path).unlink(missing_ok=True)


def _is_verified_aliyun_beijing_endpoint(base_url: object) -> bool:
    if not isinstance(base_url, str):
        return False
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and hostname is not None
        and hostname.endswith(_ALIYUN_BEIJING_HOST_SUFFIX)
        and len(hostname) > len(_ALIYUN_BEIJING_HOST_SUFFIX)
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == _ALIYUN_BEIJING_OPENAI_PATH
        and not parsed.query
        and not parsed.fragment
    )


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _tool_call_evidence(tool_call: Any) -> dict[str, Any]:
    function = _value(tool_call, "function", {})
    return {
        "id": _value(tool_call, "id"),
        "type": _value(tool_call, "type"),
        "function": {
            "name": _value(function, "name"),
            "arguments": _value(function, "arguments"),
        },
    }


def _safe_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("evidence keys must be strings")
            if _is_forbidden_evidence_key(key):
                continue
            result[key] = _safe_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported evidence value type: {type(value).__name__}")


def _is_forbidden_evidence_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in _FORBIDDEN_EVIDENCE_KEY_MARKERS)


__all__ = [
    "MODEL",
    "BudgetedAliyunClient",
    "BudgetedProviderError",
]
