"""Explicit capability for a named, local M4 non-thinking summary experiment.

No API or AgentVersion publication path creates this capability. A runner must
construct it with a digest-pinned local tokenizer, one loopback experiment
database, one frozen version/thread, and the finite set of Runs it will use.
Production service composition leaves the capability unset.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.engine import make_url

from packages.agent_runtime.deepseek_v41_counter import DeepseekV41TextCounter
from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_ID,
    SUMMARY_NON_THINKING_COUNTER_REVISION,
    SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
    SUMMARY_NON_THINKING_MODE,
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
    SUMMARY_NON_THINKING_STRATEGY_VERSION,
    SUMMARY_NON_THINKING_TOKENIZER_SHA256,
    frozen_summary_request_identity,
)
from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelRequest,
    ModelResponse,
)
from packages.model_gateway.run_budget_adapter import ModelAttemptBudgetError

_EXPERIMENT_DATABASE_PATTERNS = (
    re.compile(r"^agenthub_m4_local_experiment_[a-z0-9_]+$"),
    re.compile(r"^agenthub_run_budget_m4_\d{8}_v\d+$"),
)
_EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_REQUEST_IDENTITY = {
    "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
    "request_mode": SUMMARY_NON_THINKING_MODE,
    "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
    "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
    "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
    "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
    "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
    "request_counter_thinking": False,
}


class LocalSummaryExperimentScopeError(ValueError):
    """A local summary experiment capability is incomplete or mismatched."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExperimentAccountingHook(Protocol):
    """Synchronous adapter to the outer experiment ledger, not the Run ledger."""

    def reserve_dispatch(self, **evidence: Any) -> Any: ...

    def mark_unknown(self, reservation: Any, *, reason: str) -> None: ...

    def settle_response(
        self, reservation: Any, *, usage: Any, provider: str, model: str
    ) -> None: ...

    def report(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LocalSummaryExperimentPermit:
    """In-process capability bound to one local database, Thread, version and Runs."""

    experiment_id: str
    database_host: str
    database_port: int
    database_name: str
    workspace_id: UUID
    thread_id: UUID
    agent_version_id: UUID
    resolved_spec_hash: str
    run_ids: frozenset[UUID]
    provider: str
    model: str
    base_url: str
    output_token_limit: int
    counter: DeepseekV41TextCounter = field(repr=False, compare=False)
    accounting_hook: ExperimentAccountingHook = field(repr=False, compare=False)
    _ledger_reservations: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.experiment_id, str) or not _EXPERIMENT_ID.fullmatch(
            self.experiment_id
        ):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_ID_INVALID")
        if not _is_loopback(self.database_host):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_DATABASE_NOT_LOCAL")
        if type(self.database_port) is not int or not 1 <= self.database_port <= 65535:
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_DATABASE_INVALID")
        if not isinstance(self.database_name, str) or not any(
            pattern.fullmatch(self.database_name) for pattern in _EXPERIMENT_DATABASE_PATTERNS
        ):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_DATABASE_NOT_NAMED")
        if any(
            not isinstance(value, UUID)
            for value in (self.workspace_id, self.thread_id, self.agent_version_id)
        ):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_IDENTITY_INVALID")
        run_ids = frozenset(self.run_ids)
        if not run_ids or any(not isinstance(value, UUID) for value in run_ids):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_RUN_SCOPE_INVALID")
        object.__setattr__(self, "run_ids", run_ids)
        if not isinstance(self.resolved_spec_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.resolved_spec_hash
        ):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_SPEC_HASH_INVALID")
        if (
            self.provider != SUMMARY_NON_THINKING_PROVIDER
            or self.model != SUMMARY_NON_THINKING_MODEL
        ):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_MODEL_REJECTED")
        if not _is_beijing_route(self.base_url):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_ROUTE_REJECTED")
        if type(self.output_token_limit) is not int or not 0 < self.output_token_limit <= 2048:
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_OUTPUT_LIMIT_INVALID")
        if not isinstance(self.counter, DeepseekV41TextCounter) or not self.counter.verified_source:
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_COUNTER_UNVERIFIED")
        if not all(
            callable(getattr(self.accounting_hook, name, None))
            for name in ("reserve_dispatch", "mark_unknown", "settle_response", "report")
        ):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_ACCOUNTING_HOOK_REQUIRED")

    @classmethod
    def from_database_url(
        cls,
        *,
        experiment_id: str,
        database_url: str,
        workspace_id: UUID,
        thread_id: UUID,
        agent_version_id: UUID,
        resolved_spec_hash: str,
        run_ids: frozenset[UUID] | set[UUID] | tuple[UUID, ...],
        base_url: str,
        output_token_limit: int,
        tokenizer_path: Path,
        accounting_hook: ExperimentAccountingHook,
    ) -> LocalSummaryExperimentPermit:
        """Bind a local permit to the given DB identity and pinned local asset."""

        try:
            database = make_url(database_url)
            counter = DeepseekV41TextCounter.from_file(tokenizer_path)
        except Exception as error:
            raise LocalSummaryExperimentScopeError(
                "SUMMARY_EXPERIMENT_CONFIGURATION_INVALID"
            ) from error
        if database.host is None or database.database is None:
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_DATABASE_INVALID")
        return cls(
            experiment_id=experiment_id,
            database_host=database.host,
            database_port=database.port or 5432,
            database_name=database.database,
            workspace_id=workspace_id,
            thread_id=thread_id,
            agent_version_id=agent_version_id,
            resolved_spec_hash=resolved_spec_hash,
            run_ids=frozenset(run_ids),
            provider=SUMMARY_NON_THINKING_PROVIDER,
            model=SUMMARY_NON_THINKING_MODEL,
            base_url=base_url,
            output_token_limit=output_token_limit,
            counter=counter,
            accounting_hook=accounting_hook,
        )

    @property
    def request_identity(self) -> dict[str, Any]:
        return dict(_REQUEST_IDENTITY)

    def request_hash(
        self,
        *,
        run_id: UUID,
        attempt_kind: str,
        provider: str,
        model: str,
        request: ModelRequest,
        output_tokens: int,
        temperature: Any = None,
    ) -> str:
        return canonical_json_hash(
            {
                "version": "m4-local-experiment-dispatch-v1",
                "experiment_id": self.experiment_id,
                "run_id": str(run_id),
                "attempt_kind": attempt_kind,
                "route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
                "endpoint": self.base_url.rstrip("/"),
                "provider": provider,
                "model": model,
                "output_tokens": output_tokens,
                "temperature": _json_value(temperature),
                "request": _json_value(request),
            }
        )

    def wrap_model_attempt_budget(self, delegate: Any, *, run_id: UUID) -> Any:
        if run_id not in self.run_ids or not callable(getattr(delegate, "reserve_attempt", None)):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_BUDGET_ADAPTER_INVALID")
        return _LocalExperimentModelAttemptBudget(self, delegate, run_id=run_id)

    def matches_session_factory(self, session_factory: Any) -> bool:
        """Require the executing runtime's actual SQLAlchemy bind to match."""

        bind = getattr(session_factory, "kw", {}).get("bind")
        url = getattr(bind, "url", None)
        if url is None:
            return False
        try:
            actual = make_url(str(url))
        except Exception:
            return False
        return (
            actual.host == self.database_host
            and (actual.port or 5432) == self.database_port
            and actual.database == self.database_name
            and _is_loopback(actual.host or "")
        )

    def authorizes_endpoint(self, base_url: str | None) -> bool:
        return (
            isinstance(base_url, str)
            and _is_beijing_route(base_url)
            and _normalized_endpoint(base_url) == _normalized_endpoint(self.base_url)
        )

    def authorizes_prepared_gateway(self, gateway: Any, *, profile_id: UUID) -> bool:
        credentials = getattr(gateway, "_credentials", None)
        credential = credentials.get(profile_id) if isinstance(credentials, Mapping) else None
        return credential is not None and self.authorizes_endpoint(
            getattr(credential, "base_url", None)
        )

    def authorizes_run(
        self,
        *,
        session_factory: Any,
        workspace_id: UUID,
        thread_id: UUID | None,
        run_id: UUID,
        agent_version_id: UUID,
        resolved_spec_hash: str | None,
        spec: Any,
    ) -> bool:
        """Authorize only a predeclared Run on the frozen version and Thread."""

        if not self.matches_session_factory(session_factory):
            return False
        if (
            workspace_id != self.workspace_id
            or thread_id != self.thread_id
            or run_id not in self.run_ids
            or agent_version_id != self.agent_version_id
            or resolved_spec_hash != self.resolved_spec_hash
        ):
            return False
        try:
            identity = frozen_summary_request_identity(spec.runtime["summary_policy"])
            primary = spec.model_plan.primary
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        return (
            identity == _REQUEST_IDENTITY
            and primary.provider == self.provider
            and primary.model == self.model
            and not spec.model_plan.fallbacks
        )

    def authorizes_job(
        self,
        *,
        session_factory: Any,
        current_run_id: UUID | None,
        job: Any,
    ) -> bool:
        """Recheck the job, current Run and stable experiment identity."""

        if not self.matches_session_factory(session_factory):
            return False
        try:
            policy_identity = frozen_summary_request_identity(job.policy_spec)
            scope = job.source_scope
            spec_scope = scope.get("local_summary_experiment")
            model = job.model_spec
        except (AttributeError, TypeError, ValueError):
            return False
        return (
            current_run_id in self.run_ids
            and job.workspace_id == self.workspace_id
            and job.thread_id == self.thread_id
            and job.trigger_run_id in self.run_ids
            and policy_identity == _REQUEST_IDENTITY
            and model.get("provider") == self.provider
            and model.get("model") == self.model
            and all(model.get(key) == value for key, value in _REQUEST_IDENTITY.items())
            and spec_scope == self.source_scope_payload()
        )

    def source_scope_payload(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "database_host": self.database_host,
            "database_port": self.database_port,
            "database_name": self.database_name,
            "workspace_id": str(self.workspace_id),
            "thread_id": str(self.thread_id),
            "agent_version_id": str(self.agent_version_id),
            "resolved_spec_hash": self.resolved_spec_hash,
            # Run membership is checked against this live permit by
            # authorizes_run/authorizes_job. It is deliberately absent from
            # the stable cross-Run source identity so a later permit can add
            # the consuming Run without invalidating the published revision.
            **_REQUEST_IDENTITY,
        }

    def reserve_dispatch(self, **evidence: Any) -> Any:
        """Reserve in the shared research ledger before the provider boundary."""
        reservation = self.accounting_hook.reserve_dispatch(
            experiment_id=self.experiment_id,
            **evidence,
        )
        ledger = self._strict_ledger()
        if ledger is not None:
            attempt_id = evidence.get("attempt_id")
            request_identity = evidence.get("request_identity")
            receipts = getattr(ledger, "_reservation_receipts", None)
            ledger_receipt = receipts.get(attempt_id) if isinstance(receipts, dict) else None
            if (
                ledger_receipt is None
                or ledger_receipt.attempt_id != attempt_id
                or ledger_receipt.scope_id != self.experiment_id
                or ledger_receipt.request_identity != request_identity
            ):
                raise LocalSummaryExperimentScopeError(
                    "SUMMARY_EXPERIMENT_LEDGER_RESERVATION_RECEIPT_UNAVAILABLE"
                )
            self._ledger_reservations[attempt_id] = ledger_receipt
        return reservation

    def mark_unknown(self, reservation: Any, *, reason: str) -> None:
        self.accounting_hook.mark_unknown(reservation, reason=reason)
        attempt_id = _reservation_attempt_id(reservation)
        if attempt_id is not None:
            self._ledger_reservations.pop(attempt_id, None)

    def settle_response(
        self,
        reservation: Any,
        *,
        usage: Any,
        provider: str,
        model: str,
    ) -> None:
        """Settle only from the provider's identity and reported token usage."""
        ledger = self._strict_ledger()
        if ledger is None:
            self.accounting_hook.settle_response(
                reservation,
                usage=usage,
                provider=provider,
                model=model,
            )
            return

        attempt_id = _reservation_attempt_id(reservation)
        request_identity = _reservation_request_identity(reservation)
        ledger_receipt = self._ledger_reservations.get(attempt_id)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if (
            attempt_id is None
            or not isinstance(request_identity, str)
            or ledger_receipt is None
            or ledger_receipt.request_identity != request_identity
            or provider != self.provider
            or model != self.model
            or type(input_tokens) is not int
            or type(output_tokens) is not int
            or total_tokens != input_tokens + output_tokens
        ):
            raise LocalSummaryExperimentScopeError(
                "SUMMARY_EXPERIMENT_RESPONSE_RECEIPT_INPUT_INVALID"
            )

        row = next(
            (
                item
                for item in ledger.state.get("attempts", [])
                if item.get("attempt_id") == attempt_id
            ),
            None,
        )
        counter = row.get("input_counter") if isinstance(row, Mapping) else None
        if (
            isinstance(counter, Mapping)
            and counter.get("thinking_mode") == "NON_THINKING"
            and counter.get("tokens") != input_tokens
        ):
            self.mark_unknown(reservation, reason="provider usage differs from frozen counter")
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_PROVIDER_COUNTER_MISMATCH")

        input_price = getattr(self.accounting_hook, "_input_price", None)
        output_price = getattr(self.accounting_hook, "_output_price", None)
        if not isinstance(input_price, Decimal) or not isinstance(output_price, Decimal):
            raise LocalSummaryExperimentScopeError("SUMMARY_EXPERIMENT_LEDGER_PRICING_UNAVAILABLE")
        actual_cost = (
            Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
        ) / Decimal(1_000_000)
        result_identity = canonical_json_hash(
            {
                "version": "m4-local-experiment-response-v1",
                "request_identity": request_identity,
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_cny": str(actual_cost),
            }
        )
        if row is not None and row.get("status") == "SETTLED":
            if (
                row.get("request_identity") == request_identity
                and row.get("result_identity") == result_identity
                and row.get("charged_or_reserved_cny") == str(actual_cost)
            ):
                return
            raise LocalSummaryExperimentScopeError(
                "SUMMARY_EXPERIMENT_SETTLEMENT_IDENTITY_MISMATCH"
            )
        response_receipt = ledger.issue_response_receipt(
            ledger_receipt,
            result_identity=result_identity,
            conservative_cost_cny=actual_cost,
        )
        ledger.settle(
            attempt_id,
            conservative_cost_cny=actual_cost,
            result_identity=result_identity,
            reservation_receipt=ledger_receipt,
            response_receipt=response_receipt,
        )

    def _strict_ledger(self) -> Any | None:
        ledger = getattr(self.accounting_hook, "_budget", None)
        if all(
            callable(getattr(ledger, method, None))
            for method in ("issue_response_receipt", "settle")
        ):
            return ledger
        return None

    def accounting_report(self) -> Mapping[str, Any]:
        return self.accounting_hook.report()


def _is_loopback(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _normalized_endpoint(value: str) -> tuple[str, str, int | None, str] | None:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port,
            parsed.path.rstrip("/"),
        )
    except ValueError:
        return None


def _reservation_attempt_id(reservation: Any) -> str | None:
    value = (
        reservation.get("attempt_id")
        if isinstance(reservation, Mapping)
        else getattr(reservation, "attempt_id", None)
    )
    return value if isinstance(value, str) and value else None


def _reservation_request_identity(reservation: Any) -> str | None:
    value = (
        reservation.get("request_identity")
        if isinstance(reservation, Mapping)
        else getattr(reservation, "request_identity", None)
    )
    return value if isinstance(value, str) and value else None


def _json_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_value(item) for item in value]
        return sorted(normalized, key=canonical_json)
    raise TypeError("experiment request contains a non-canonical value")


@dataclass(slots=True)
class _LocalAttemptRecord:
    run_reservation: Any
    experiment_reservation: Any
    provider: str
    model: str


class _LocalExperimentModelAttemptBudget:
    """Compose each normal gateway attempt with the shared experiment ledger."""

    def __init__(self, permit: LocalSummaryExperimentPermit, delegate: Any, *, run_id: UUID):
        self._permit = permit
        self._delegate = delegate
        self._run_id = run_id
        self._records: dict[str, _LocalAttemptRecord] = {}

    def is_bound_to_run(self, workspace_id: UUID, run_id: UUID, call_identity: str) -> bool:
        return (
            run_id == self._run_id
            and workspace_id == self._permit.workspace_id
            and bool(call_identity)
            and self._delegate.is_bound_to_run(workspace_id, run_id, call_identity)
        )

    async def reserve_attempt(self, context, profile, request, **kwargs):
        reservation = await self._delegate.reserve_attempt(context, profile, request, **kwargs)
        if not isinstance(request, ModelRequest):
            await self._mark_run_budget_unknown(reservation)
            raise ModelAttemptBudgetError(
                "LOCAL_EXPERIMENT_REQUEST_INVALID", "A model request is required for accounting."
            )
        provider = str(kwargs.get("provider", ""))
        model = str(getattr(profile, "model", ""))
        attempt_kind = str(getattr(kwargs.get("attempt_kind"), "value", kwargs.get("attempt_kind")))
        attempt_id = (
            "m4-"
            + canonical_json_hash(
                {
                    "run": str(self._run_id),
                    "identity": reservation.attempt_identity,
                }
            )[:60]
        )
        try:
            request_identity = self._permit.request_hash(
                run_id=self._run_id,
                attempt_kind=attempt_kind,
                provider=provider,
                model=model,
                request=request,
                output_tokens=int(getattr(profile, "max_tokens", 0)),
                temperature=getattr(profile, "temperature", None),
            )
            input_counter = self._input_counter(request, request_identity)
            outer = self._permit.reserve_dispatch(
                attempt_id=attempt_id,
                run_id=self._run_id,
                attempt_kind=attempt_kind,
                provider=provider,
                model=model,
                request_identity=request_identity,
                estimated_input_tokens=32768,
                requested_output_tokens=int(getattr(profile, "max_tokens", 0)),
                maximum_cost_cny=reservation.cost_upper_bound.amount,
                currency=reservation.cost_upper_bound.currency,
                input_counter=input_counter,
            )
        except Exception as error:
            await self._mark_run_budget_unknown(reservation)
            raise ModelAttemptBudgetError(
                "LOCAL_EXPERIMENT_LEDGER_RESERVATION_FAILED",
                "The local experiment ledger rejected the model attempt.",
            ) from error
        self._records[reservation.attempt_identity] = _LocalAttemptRecord(
            reservation, outer, provider, model
        )
        return reservation

    async def mark_unknown_outcome(self, reservation: Any) -> None:
        record = self._records.pop(reservation.attempt_identity, None)
        await self._delegate.mark_unknown_outcome(reservation)
        if record is not None:
            self._permit.mark_unknown(
                record.experiment_reservation,
                reason="provider outcome could not be confirmed",
            )

    async def record_response(self, response: ModelResponse) -> None:
        if not isinstance(response, ModelResponse) or not self._records:
            return
        identity = next(reversed(self._records))
        record = self._records.pop(identity)
        if response.provider != record.provider or response.model != record.model:
            self._permit.mark_unknown(
                record.experiment_reservation,
                reason="provider response identity did not match the reserved attempt",
            )
            return
        usage = response.usage
        if usage is None:
            self._permit.mark_unknown(
                record.experiment_reservation,
                reason="provider response did not include token usage",
            )
            return
        self._permit.settle_response(
            record.experiment_reservation,
            usage=usage,
            provider=record.provider,
            model=record.model,
        )

    async def _mark_run_budget_unknown(self, reservation: Any) -> None:
        await self._delegate.mark_unknown_outcome(reservation)

    def _input_counter(self, request: ModelRequest, identity: str) -> dict[str, Any] | None:
        if request.request_mode is not AliyunDeepSeekRequestMode.NON_THINKING:
            return None
        try:
            tokens = self._permit.counter.count_frozen_summary_request(
                request,
                request_identity=self._permit.request_identity,
                provider=SUMMARY_NON_THINKING_PROVIDER,
                model=SUMMARY_NON_THINKING_MODEL,
            )
        except Exception as error:
            raise ModelAttemptBudgetError(
                "LOCAL_EXPERIMENT_COUNTER_REJECTED",
                "The request is outside the verified NON_THINKING counter shape.",
            ) from error
        if not 0 < tokens <= 32768:
            raise ModelAttemptBudgetError(
                "LOCAL_EXPERIMENT_COUNTER_INVALID", "The measured input is outside its limit."
            )
        return {
            "counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
            "tokenizer_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
            "thinking_mode": "NON_THINKING",
            "tokens": tokens,
            "input_limit": 32768,
            "request_identity": identity,
        }


def _is_beijing_route(base_url: str) -> bool:
    if not isinstance(base_url, str) or not base_url:
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
        and hostname.endswith(".cn-beijing.maas.aliyuncs.com")
        and len(hostname) > len(".cn-beijing.maas.aliyuncs.com")
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == "/compatible-mode/v1"
        and not parsed.query
        and not parsed.fragment
    )


__all__ = [
    "ExperimentAccountingHook",
    "LocalSummaryExperimentPermit",
    "LocalSummaryExperimentScopeError",
]
