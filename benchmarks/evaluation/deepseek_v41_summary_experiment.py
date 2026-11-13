"""Local-only admission for controlled DeepSeek V4.1 summary experiments.

This helper is deliberately independent from AgentVersion publication and the
production summary dispatch guard. It verifies a local pinned tokenizer, one
Aliyun Beijing OpenAI-compatible route, the frozen DeepSeek V4.1 Flash model,
NON_THINKING mode, and the complete system/user request before returning an
input token count. It never creates a gateway or performs network I/O.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from benchmarks.evaluation.memory_trial_budget import (
    LOCAL_ROUND_ATTEMPT_LIMIT,
    RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT,
    MemoryTrialBudget,
    upper_bound_cost_cny,
)
from benchmarks.evaluation.memory_trial_client import PRICE_IDENTITY
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
    _parse_summary_policy,
    parse_frozen_agent_spec,
)
from packages.agent_runtime.local_summary_experiment import LocalSummaryExperimentPermit
from packages.agent_runtime.models import Agent, AgentVersion
from packages.agent_runtime.publish import SUMMARY_SPEC_SCHEMA_VERSION
from packages.control_plane.models import WorkspaceMembership
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import AliyunDeepSeekRequestMode, ModelRequest

_BEIJING_HOST_SUFFIX = ".cn-beijing.maas.aliyuncs.com"
_BEIJING_OPENAI_PATH = "/compatible-mode/v1"
_EXPERIMENT_DATABASE_PATTERNS = (re.compile(r"agenthub_run_budget_m4_20261028_v4"),)
DEFAULT_TOKENIZER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".scratch"
    / "model-assets"
    / "deepseek-v4.1-flash-1017"
    / SUMMARY_NON_THINKING_COUNTER_REVISION
    / "tokenizer.json"
)

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


class SummaryExperimentAdmissionError(ValueError):
    """A local summary request failed its fixed experiment admission gate."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AdmittedSummaryExperimentRequest:
    """Offline evidence required by a later runner before it can dispatch."""

    route_allowlist: str
    provider: str
    model: str
    request_mode: str
    request_counter_id: str
    request_counter_revision: str
    request_counter_runtime_version: str
    request_counter_sha256: str
    thinking: bool
    input_tokens: int
    input_token_limit: int
    output_tokens_reserved: int


@dataclass(frozen=True, slots=True)
class ExperimentLedgerReservation:
    attempt_id: str
    request_identity: str
    reserved_cost_cny: Decimal


class MemoryTrialBudgetAccountingHook:
    """Bridge every local provider dispatch to A's existing v2 research ledger."""

    def __init__(
        self,
        budget: MemoryTrialBudget,
        *,
        scope_id: str,
        input_price_per_million: Decimal = Decimal("2"),
        output_price_per_million: Decimal = Decimal("8"),
    ) -> None:
        if not isinstance(budget, MemoryTrialBudget):
            raise TypeError("budget must be the owned MemoryTrialBudget instance")
        if budget.state.get("price_identity") != PRICE_IDENTITY:
            raise ValueError("MEMORY_TRIAL_PRICE_IDENTITY_MISMATCH")
        if not scope_id or budget.state.get("local_experiment_policy", {}).get(
            "active_scope_id"
        ) != scope_id:
            raise ValueError("MEMORY_TRIAL_ACTIVE_SCOPE_MISMATCH")
        if budget.state.get("local_experiment_policy", {}).get("mode") != (
            RESERVE_UNKNOWN_UPPER_BOUND_ALLOW_INDEPENDENT
        ):
            raise ValueError("MEMORY_TRIAL_INDEPENDENT_POLICY_REQUIRED")
        self._budget = budget
        self.scope_id = scope_id
        self._input_price = input_price_per_million
        self._output_price = output_price_per_million

    def reserve_dispatch(
        self,
        *,
        experiment_id: str,
        attempt_id: str,
        run_id: UUID,
        attempt_kind: str,
        provider: str,
        model: str,
        request_identity: str,
        estimated_input_tokens: int,
        requested_output_tokens: int,
        maximum_cost_cny: Decimal,
        currency: str,
        input_counter: dict | None,
    ) -> ExperimentLedgerReservation:
        if experiment_id != self.scope_id or not run_id or not attempt_kind:
            raise ValueError("MEMORY_TRIAL_DISPATCH_SCOPE_MISMATCH")
        if estimated_input_tokens != 32768:
            raise ValueError("MEMORY_TRIAL_FULL_WINDOW_RESERVATION_REQUIRED")
        if provider != SUMMARY_NON_THINKING_PROVIDER or model != SUMMARY_NON_THINKING_MODEL:
            raise ValueError("MEMORY_TRIAL_MODEL_ROUTE_MISMATCH")
        if currency != "CNY" or not isinstance(maximum_cost_cny, Decimal):
            raise ValueError("MEMORY_TRIAL_PRICE_CURRENCY_MISMATCH")
        if not isinstance(request_identity, str) or len(request_identity) != 64:
            raise ValueError("INVALID_MEMORY_TRIAL_REQUEST_IDENTITY")
        if type(requested_output_tokens) is not int or not 0 < requested_output_tokens <= 2048:
            raise ValueError("INVALID_MEMORY_TRIAL_OUTPUT_LIMIT")
        conservative_bound = upper_bound_cost_cny(
            input_token_bound=estimated_input_tokens,
            output_token_bound=requested_output_tokens,
            input_per_million=self._input_price,
            output_per_million=self._output_price,
        )
        reserved_cost = max(maximum_cost_cny, conservative_bound)
        self._budget.reserve(
            attempt_id,
            stage="M4",
            estimated_input_tokens=estimated_input_tokens,
            requested_output_tokens=requested_output_tokens,
            upper_cost_cny=reserved_cost,
            input_counter=input_counter,
            request_identity=request_identity,
        )
        return ExperimentLedgerReservation(attempt_id, request_identity, reserved_cost)

    def mark_unknown(self, reservation: ExperimentLedgerReservation | dict, *, reason: str) -> None:
        del reason
        reservation = _ledger_reservation(reservation)
        row = next(
            (
                item
                for item in self._budget.state["attempts"]
                if item["attempt_id"] == reservation.attempt_id
            ),
            None,
        )
        if row is None or row.get("request_identity") != reservation.request_identity:
            raise ValueError("MEMORY_TRIAL_RESERVATION_IDENTITY_MISMATCH")
        # Reserved/unknown is the conservative terminal hold; never retry it.

    def settle_response(
        self,
        reservation: ExperimentLedgerReservation | dict,
        *,
        usage,
        provider: str,
        model: str,
    ) -> None:
        reservation = _ledger_reservation(reservation)
        if provider != SUMMARY_NON_THINKING_PROVIDER or model != SUMMARY_NON_THINKING_MODEL:
            self.mark_unknown(reservation, reason="response model mismatch")
            raise ValueError("MEMORY_TRIAL_RESPONSE_MODEL_MISMATCH")
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or total_tokens != input_tokens + output_tokens
        ):
            self.mark_unknown(reservation, reason="provider usage is unavailable")
            return
        actual_cost = (
            Decimal(input_tokens) * self._input_price
            + Decimal(output_tokens) * self._output_price
        ) / Decimal(1_000_000)
        result_identity = canonical_json_hash(
            {
                "version": "m4-local-experiment-response-v1",
                "request_identity": reservation.request_identity,
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cost_cny": str(actual_cost),
            }
        )
        existing = next(
            (
                row
                for row in self._budget.state["attempts"]
                if row["attempt_id"] == reservation.attempt_id
            ),
            None,
        )
        if existing is None or existing.get("request_identity") != reservation.request_identity:
            raise ValueError("MEMORY_TRIAL_RESERVATION_IDENTITY_MISMATCH")
        if existing.get("status") == "SETTLED":
            if existing.get("result_identity") != result_identity:
                raise ValueError("MEMORY_TRIAL_SETTLEMENT_IDENTITY_MISMATCH")
            return
        self._budget.settle(
            reservation.attempt_id,
            conservative_cost_cny=actual_cost,
            result_identity=result_identity,
        )

    def report(self) -> dict[str, object]:
        attempts = self._budget.state.get("attempts", [])
        policy = self._budget.state.get("local_experiment_policy", {})
        return {
            "scope_id": self.scope_id,
            "attempt_count": len(attempts),
            "reserved_or_unknown_count": sum(
                row.get("status") == "RESERVED_OR_UNKNOWN" for row in attempts
            ),
            "settled_count": sum(row.get("status") == "SETTLED" for row in attempts),
            "charged_or_reserved_cny": str(
                sum(
                    (Decimal(row["charged_or_reserved_cny"]) for row in attempts),
                    Decimal(0),
                )
            ),
            "round_attempts_used": policy.get("round_attempts_used"),
            "round_attempt_limit": policy.get("round_attempt_limit", LOCAL_ROUND_ATTEMPT_LIMIT),
        }


async def register_local_v2_summary_version(
    session: AsyncSession,
    *,
    base_version_id: UUID,
    workspace_id: UUID,
    created_by: UUID,
) -> AgentVersion:
    """Register an immutable v2 copy only inside a named loopback experiment DB.

    This helper does not relax AgentVersionService.publish or any API contract.
    The caller owns the transaction and must still point its experiment Thread
    at the returned version. The returned resolved spec is re-parsed and
    hash-bound before insertion.
    """

    try:
        bind = session.get_bind()
        database = make_url(str(bind.url))
    except Exception as error:
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_DATABASE_INVALID") from error
    if (
        database.host not in {"127.0.0.1", "localhost", "::1"}
        or database.database is None
        or not any(
            pattern.fullmatch(database.database) for pattern in _EXPERIMENT_DATABASE_PATTERNS
        )
    ):
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_DATABASE_NOT_ISOLATED")

    membership = await session.scalar(
        select(WorkspaceMembership.workspace_id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == created_by,
            WorkspaceMembership.role == "DEVELOPER",
        )
    )
    if membership is None:
        raise AgentHubError("FORBIDDEN", "Current workspace edit permission is required.", 403)
    base = await session.scalar(
        select(AgentVersion)
        .where(
            AgentVersion.workspace_id == workspace_id,
            AgentVersion.id == base_version_id,
        )
        .with_for_update(read=True)
    )
    if base is None or canonical_json_hash(base.resolved_spec) != base.resolved_spec_hash:
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_BASE_VERSION_INVALID")
    frozen = parse_frozen_agent_spec(base.resolved_spec, workspace_id=workspace_id)
    if frozen.model_plan.fallbacks:
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_FALLBACKS_UNSUPPORTED")
    if (
        frozen.model_plan.primary.provider != SUMMARY_NON_THINKING_PROVIDER
        or frozen.model_plan.primary.model != SUMMARY_NON_THINKING_MODEL
    ):
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_MODEL_REJECTED")
    raw = copy.deepcopy(base.resolved_spec)
    # The ordinary published base is schema v4 because its quality gate keeps
    # summaries absent. The isolated v2 clone is the first schema-v5 spec.
    raw["spec_schema_version"] = SUMMARY_SPEC_SCHEMA_VERSION
    runtime = raw.get("runtime")
    current_policy = runtime.get("summary_policy") if isinstance(runtime, dict) else None
    if current_policy is None:
        # Publication intentionally rejects summary policies while the quality
        # gate is closed. Seed the fixed baseline only in this local v2 clone.
        current_policy = {
            "enabled": True,
            "policy_version": "thread_summary_policy_v1",
            "prompt_version": "summary_candidate_zh_v1",
            "validator_version": "summary_mechanical_v1",
            "tokenizer_id": "approx_language_v1",
            "tokenizer_version": 1,
            "soft_trigger_percent": 70,
            "hard_trigger_percent": 85,
            "target_percent": 55,
        }
    if not isinstance(current_policy, dict) or current_policy.get("enabled") is not True:
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_SUMMARY_POLICY_REQUIRED")
    request_policy = {
        **current_policy,
        **_REQUEST_IDENTITY,
    }
    _parse_summary_policy(request_policy, primary=frozen.model_plan.primary)
    runtime["summary_policy"] = request_policy
    parsed_copy = parse_frozen_agent_spec(raw, workspace_id=workspace_id)
    if parsed_copy.runtime["summary_policy"] != request_policy:
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_FROZEN_POLICY_MISMATCH")
    version_number = (
        await session.scalar(
            select(func.max(AgentVersion.version_number)).where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.agent_id == base.agent_id,
            )
        )
        or 0
    ) + 1
    agent = await session.scalar(
        select(Agent)
        .where(Agent.workspace_id == workspace_id, Agent.id == base.agent_id)
        .with_for_update()
    )
    if agent is None:
        raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_AGENT_MISSING")
    version = AgentVersion(
        workspace_id=workspace_id,
        agent_id=base.agent_id,
        version_number=version_number,
        spec_schema_version=raw["spec_schema_version"],
        resolved_spec=raw,
        resolved_spec_hash=canonical_json_hash(raw),
        created_by=created_by,
    )
    session.add(version)
    await session.flush()
    return version


class DeepseekV41SummaryExperimentGate:
    """Count and admit one complete, offline summary request."""

    def __init__(
        self,
        counter: DeepseekV41TextCounter,
        *,
        output_token_limit: int,
    ) -> None:
        if not isinstance(counter, DeepseekV41TextCounter) or not counter.verified_source:
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_COUNTER_UNVERIFIED")
        if type(output_token_limit) is not int or not 0 < output_token_limit <= 2048:
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_OUTPUT_LIMIT_INVALID")
        self._counter = counter
        self._output_token_limit = output_token_limit

    @property
    def counter(self) -> DeepseekV41TextCounter:
        return self._counter

    def bind_runtime_permit(
        self,
        *,
        experiment_id: str,
        database_url: str,
        workspace_id: UUID,
        thread_id: UUID,
        agent_version_id: UUID,
        resolved_spec_hash: str,
        run_ids: set[UUID] | frozenset[UUID] | tuple[UUID, ...],
        base_url: str,
        accounting_hook,
    ) -> LocalSummaryExperimentPermit:
        """Create the exact DB/version/Thread/Run capability for a local runner."""

        try:
            database = make_url(database_url)
            return LocalSummaryExperimentPermit(
                experiment_id=experiment_id,
                database_host=database.host or "",
                database_port=database.port or 5432,
                database_name=database.database or "",
                workspace_id=workspace_id,
                thread_id=thread_id,
                agent_version_id=agent_version_id,
                resolved_spec_hash=resolved_spec_hash,
                run_ids=frozenset(run_ids),
                provider=SUMMARY_NON_THINKING_PROVIDER,
                model=SUMMARY_NON_THINKING_MODEL,
                base_url=base_url,
                output_token_limit=self._output_token_limit,
                counter=self._counter,
                accounting_hook=accounting_hook,
            )
        except Exception as error:
            if isinstance(error, SummaryExperimentAdmissionError):
                raise
            raise SummaryExperimentAdmissionError(
                "SUMMARY_EXPERIMENT_SCOPE_INVALID"
            ) from error

    @classmethod
    def from_local_asset(
        cls,
        path: Path = DEFAULT_TOKENIZER_PATH,
        *,
        output_token_limit: int,
    ) -> DeepseekV41SummaryExperimentGate:
        """Load a fixed-hash local asset; never fetch or resolve it remotely."""

        try:
            counter = DeepseekV41TextCounter.from_file(path)
        except Exception as error:
            raise SummaryExperimentAdmissionError(
                "SUMMARY_EXPERIMENT_COUNTER_UNAVAILABLE"
            ) from error
        return cls(counter, output_token_limit=output_token_limit)

    def admit(
        self,
        request: ModelRequest,
        *,
        route_allowlist: str,
        base_url: str,
        provider: str,
        model: str,
        input_token_limit: int,
        requested_output_tokens: int,
    ) -> AdmittedSummaryExperimentRequest:
        """Return the exact offline count or reject before any runner dispatch."""

        if route_allowlist != SUMMARY_NON_THINKING_ROUTE_ALLOWLIST or not _is_beijing_route(
            base_url
        ):
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_ROUTE_REJECTED")
        if provider != SUMMARY_NON_THINKING_PROVIDER or model != SUMMARY_NON_THINKING_MODEL:
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_MODEL_REJECTED")
        if (
            not isinstance(request, ModelRequest)
            or request.request_mode is not AliyunDeepSeekRequestMode.NON_THINKING
            or request.retry_policy.max_attempts != 1
            or len(request.messages) != 2
            or tuple(message.role for message in request.messages) != ("system", "user")
        ):
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_REQUEST_REJECTED")
        if (
            type(input_token_limit) is not int
            or input_token_limit <= 0
            or type(requested_output_tokens) is not int
            or requested_output_tokens <= 0
            or requested_output_tokens > self._output_token_limit
        ):
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_BUDGET_INVALID")
        try:
            input_tokens = self._counter.count_frozen_summary_request(
                request,
                request_identity=_REQUEST_IDENTITY,
                provider=provider,
                model=model,
            )
        except Exception as error:
            raise SummaryExperimentAdmissionError(
                "SUMMARY_EXPERIMENT_COUNTER_FAILED"
            ) from error
        if type(input_tokens) is not int or input_tokens <= 0:
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_COUNTER_INVALID")
        if input_tokens > input_token_limit:
            raise SummaryExperimentAdmissionError("SUMMARY_EXPERIMENT_INPUT_BUDGET_EXCEEDED")
        return AdmittedSummaryExperimentRequest(
            route_allowlist=SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
            provider=SUMMARY_NON_THINKING_PROVIDER,
            model=SUMMARY_NON_THINKING_MODEL,
            request_mode=SUMMARY_NON_THINKING_MODE,
            request_counter_id=SUMMARY_NON_THINKING_COUNTER_ID,
            request_counter_revision=SUMMARY_NON_THINKING_COUNTER_REVISION,
            request_counter_runtime_version=SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
            request_counter_sha256=SUMMARY_NON_THINKING_TOKENIZER_SHA256,
            thinking=False,
            input_tokens=input_tokens,
            input_token_limit=input_token_limit,
            output_tokens_reserved=requested_output_tokens,
        )


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
        and hostname.endswith(_BEIJING_HOST_SUFFIX)
        and len(hostname) > len(_BEIJING_HOST_SUFFIX)
        and port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == _BEIJING_OPENAI_PATH
        and not parsed.query
        and not parsed.fragment
    )


__all__ = [
    "AdmittedSummaryExperimentRequest",
    "DEFAULT_TOKENIZER_PATH",
    "DeepseekV41SummaryExperimentGate",
    "ExperimentLedgerReservation",
    "MemoryTrialBudgetAccountingHook",
    "SummaryExperimentAdmissionError",
]


def _ledger_reservation(value: ExperimentLedgerReservation | dict) -> ExperimentLedgerReservation:
    if isinstance(value, ExperimentLedgerReservation):
        return value
    if isinstance(value, dict):
        try:
            return ExperimentLedgerReservation(
                attempt_id=value["attempt_id"],
                request_identity=value["request_identity"],
                reserved_cost_cny=Decimal(value["reserved_cost_cny"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("MEMORY_TRIAL_RESERVATION_INVALID") from error
    raise ValueError("MEMORY_TRIAL_RESERVATION_INVALID")
