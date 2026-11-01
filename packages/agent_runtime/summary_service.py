"""Durable, single-dispatch persistence for M4 thread summaries.

The service owns database state and fences only.  It never constructs or
invokes a model gateway.  A worker commits a dispatch intent, calls its
provider adapter, and reports the returned response here.

All state-changing operations use short explicit transactions.  Every exact
job lookup includes workspace, thread and job identity; competing workers
serialize on the job row and must present the current unexpired lease token.
Publication rechecks trusted server-side bindings while holding the job row
and uses compare-and-set updates for both the artifact and job. Job creation
and every pre-provider transition also reload the persisted exact LangGraph
checkpoint through the framework adapter; request-supplied selectors are not
treated as proof.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_MODE,
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    frozen_summary_request_identity,
)
from packages.agent_runtime.history_checkpoint import (
    ExactCheckpointAdapter,
    PersistedCheckpointBinding,
    VerifiedCheckpointBinding,
    verify_exact_checkpoint_binding,
    verify_fixed_checkpoint_binding,
)
from packages.agent_runtime.local_summary_experiment import LocalSummaryExperimentPermit
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.summary_models import (
    ThreadSummaryArtifact,
    ThreadSummaryArtifactStatus,
    ThreadSummaryDispatch,
    ThreadSummaryDispatchOutcome,
    ThreadSummaryJob,
    ThreadSummaryJobSource,
    ThreadSummaryResponse,
)
from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    SummaryPrompt,
    build_summary_prompt,
)
from packages.agent_runtime.summary_state import (
    SummaryJobStatus,
    SummaryState,
    claim,
    claim_recovery,
    commit_dispatch_intent,
    fail_before_dispatch,
    publish,
    recover_expired_lease,
    store_response,
    validate_response,
)
from packages.agent_runtime.summary_validation import (
    SummaryValidationResult,
    validate_summary_candidate,
)
from packages.agent_runtime.work_layer import PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.models import PricingSnapshot
from packages.threads.models import AgentThread

logger = logging.getLogger(__name__)

SUMMARY_LEDGER_VERSION = "thread_summary_ledger_v1"
SUMMARY_JOB_IDENTITY_VERSION = "thread_summary_job_identity_v2"
SUMMARY_ARTIFACT_VERSION = "thread_summary_artifact_v1"
SUMMARY_SOURCE_INDEX_VERSION = "thread_summary_source_index_v1"
DEFAULT_LEASE_SECONDS = 180
DEFAULT_MAX_RECOVERY_BATCH = 100
MAX_SOURCES_PER_JOB = 500
MAX_RESPONSE_BYTES = 1_048_576
MAX_SUMMARY_MODEL_ROUND = 2_147_483_647


class _SummaryCounter:
    """Approximate counter for summary artifact validation, not request admission."""

    def count(self, text: str) -> int:
        return ApproxTokenEstimator().estimate(text)


class SummaryServiceError(Exception):
    """A durable summary operation was rejected without weakening fencing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SummaryVerifierPhase(StrEnum):
    DISPATCH = "DISPATCH"
    RESPONSE_VALIDATION = "RESPONSE_VALIDATION"
    PUBLICATION = "PUBLICATION"


class SummaryTrustStatus(StrEnum):
    VERIFIED = "VERIFIED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SummaryJobKey:
    """The complete tenant boundary for every durable lookup."""

    workspace_id: UUID
    thread_id: UUID
    job_id: UUID

    def __post_init__(self) -> None:
        for name in ("workspace_id", "thread_id", "job_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")


@dataclass(frozen=True, slots=True)
class NewSummarySource:
    """One source coordinate; raw Thread text is intentionally absent."""

    source_id: str
    turn_id: UUID
    run_id: UUID
    sequence: int
    original_ref: str
    source_identity_hash: str
    revision_sha256: str
    user_input_sha256: str
    final_output_sha256: str
    summary_eligibility: str
    eligibility_reason: str
    artifact_identities: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class NewSummaryJob:
    """Summary creation command and untrusted exact-checkpoint selector.

    ``checkpoint_id`` and ``checkpoint_ns`` select the tuple to read. The
    service derives the persisted checkpoint identity and model round from the
    real tuple. ``model_round`` is an optional compatibility assertion and is
    never used as evidence.
    """

    workspace_id: UUID
    thread_id: UUID
    trigger_run_id: UUID
    source_watermark: int
    source_scope: Mapping[str, Any]
    policy_spec: Mapping[str, Any]
    policy_version: str
    prompt_version: str
    budget_version: str
    model_spec: Mapping[str, Any]
    tokenizer_id: str
    tokenizer_version: str
    target_budget_tokens: int
    permission_snapshot_hash: str
    created_by: UUID
    sources: tuple[NewSummarySource, ...]
    parent_artifact_id: UUID | None = None
    parent_revision_hash: str | None = None
    checkpoint_id: str | None = None
    checkpoint_ns: str | None = None
    model_round: int | None = None


@dataclass(frozen=True, slots=True)
class CreatedSummaryJob:
    key: SummaryJobKey
    identity_hash: str
    status: str
    created: bool


@dataclass(frozen=True, slots=True)
class ClaimedSummaryJob:
    key: SummaryJobKey
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveredSummaryJob:
    key: SummaryJobKey
    previous_status: str
    current_status: str


@dataclass(frozen=True, slots=True)
class PreparedSummaryDispatch:
    """Server-verified prompt and immutable request identity before reservation."""

    key: SummaryJobKey
    created_by: UUID
    policy_version: str
    prompt_version: str
    tokenizer_id: str
    tokenizer_version: str
    prompt: SummaryPrompt
    request_spec: Mapping[str, Any]
    model_spec: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SummaryDispatchIntent:
    key: SummaryJobKey
    dispatch_id: UUID
    reservation_id: UUID
    request_hash: str
    intent_hash: str
    pricing_snapshot_id: UUID
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_amount: Decimal
    reserved_currency: str


@dataclass(frozen=True, slots=True)
class SummaryResponseData:
    """Provider output and usage recorded after, never before, storage."""

    provider: str
    model: str
    raw_response: Mapping[str, Any] | str | bytes
    usage_metadata: Mapping[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    cost_is_estimate: bool | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StoredSummaryResponse:
    key: SummaryJobKey
    response_id: UUID
    response_sha256: str
    status: str


@dataclass(frozen=True, slots=True)
class SummarySettlement:
    key: SummaryJobKey
    status: str
    response_id: UUID | None
    artifact_id: UUID | None
    validation_result: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PublishedSummaryRevision:
    """One exact published revision whose stored integrity has been rechecked."""

    key: SummaryJobKey
    artifact_id: UUID
    response_id: UUID
    revision_number: int
    revision_hash: str
    content_hash: str
    summary_content: Mapping[str, Any]
    source_index: tuple[Mapping[str, Any], ...]
    validation_result: Mapping[str, Any]
    policy_version: str
    prompt_version: str
    source_scope: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SummaryJobBinding:
    """Immutable identity handed to a trusted server-side binding verifier."""

    workspace_id: UUID
    thread_id: UUID
    job_id: UUID
    trigger_run_id: UUID
    created_by: UUID
    identity_hash: str
    source_watermark: int
    source_scope: Mapping[str, Any]
    source_scope_hash: str
    source_content_hash: str
    parent_artifact_id: UUID | None
    parent_revision_hash: str | None
    policy_version: str
    prompt_version: str
    model_spec_hash: str
    permission_snapshot_hash: str
    response_id: UUID | None
    artifact_id: UUID | None
    phase: SummaryVerifierPhase
    model_round: int | None = None
    checkpoint_id: str | None = None
    checkpoint_ns: str | None = None
    checkpoint_model_round_count: int | None = None
    checkpoint_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        checkpoint_fields = (
            self.checkpoint_id,
            self.checkpoint_ns,
            self.checkpoint_model_round_count,
            self.checkpoint_binding_sha256,
        )
        if any(value is not None for value in checkpoint_fields) and any(
            value is None for value in checkpoint_fields
        ):
            raise ValueError("exact checkpoint binding fields must be all-or-none")
        if self.checkpoint_id is None:
            return
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id:
            raise ValueError("checkpoint_id must be a non-empty string")
        if not isinstance(self.checkpoint_ns, str):
            raise ValueError("checkpoint_ns must be a string")
        if (
            isinstance(self.checkpoint_model_round_count, bool)
            or not isinstance(self.checkpoint_model_round_count, int)
            or not 0 <= self.checkpoint_model_round_count < MAX_SUMMARY_MODEL_ROUND
        ):
            raise ValueError("checkpoint_model_round_count is outside supported bounds")
        if not _sha256(self.checkpoint_binding_sha256):
            raise ValueError("checkpoint_binding_sha256 must be lowercase SHA-256")
        if self.model_round != self.checkpoint_model_round_count + 1:
            raise ValueError("model_round must follow the checkpoint round count")


@dataclass(frozen=True, slots=True)
class SummaryTrustDecision:
    """A decision from trusted server code, never a request authorization flag."""

    status: SummaryTrustStatus
    reason_codes: tuple[str, ...] = ()
    validation_context: SummaryInputContext | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SummaryTrustStatus):
            raise TypeError("status must be SummaryTrustStatus")
        reasons = tuple(self.reason_codes)
        if any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ValueError("reason_codes must contain non-empty strings")
        object.__setattr__(self, "reason_codes", reasons)

    @classmethod
    def verified(
        cls,
        validation_context: SummaryInputContext | None = None,
    ) -> SummaryTrustDecision:
        return cls(SummaryTrustStatus.VERIFIED, validation_context=validation_context)

    @classmethod
    def denied(cls, *reason_codes: str) -> SummaryTrustDecision:
        return cls(SummaryTrustStatus.DENIED, tuple(reason_codes))

    @classmethod
    def unavailable(cls, *reason_codes: str) -> SummaryTrustDecision:
        return cls(SummaryTrustStatus.UNAVAILABLE, tuple(reason_codes))


class _SummaryBindingRejected(Exception):
    """A trusted source recheck found a durable binding mismatch."""

    def __init__(self, *reason_codes: str) -> None:
        super().__init__("The frozen summary binding was rejected.")
        self.reason_codes = tuple(reason_codes) or ("SUMMARY_BINDING_REJECTED",)


SummaryContextLoader = Callable[[AsyncSession, SummaryJobBinding], Awaitable[SummaryInputContext]]


class SummaryBindingVerifier(Protocol):
    """Trusted server service that reloads the binding from authoritative data.

    Implementations may use the supplied session to lock or query their own
    source and permission records.  They must not trust request-provided
    booleans or cached M3 eligibility claims as current authorization.
    """

    async def verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision: ...


class DenyAllSummaryBindingVerifier:
    """Production-safe default until an authoritative integration exists."""

    async def verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision:
        del session, binding
        return SummaryTrustDecision.unavailable("TRUSTED_VERIFIER_NOT_CONFIGURED")


class FrozenHistorySummaryBindingVerifier:
    """Fail closed unless an authoritative loader rebuilds the frozen source view."""

    def __init__(self, context_loader: SummaryContextLoader) -> None:
        if not callable(context_loader):
            raise TypeError("context_loader must be callable")
        self._context_loader = context_loader

    async def verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision:
        try:
            context = await self._context_loader(session, binding)
        except _SummaryBindingRejected as error:
            return SummaryTrustDecision.denied(*error.reason_codes)
        except Exception:
            return SummaryTrustDecision.unavailable("TRUSTED_SOURCE_RECHECK_FAILED")
        if not isinstance(context, SummaryInputContext):
            return SummaryTrustDecision.unavailable("TRUSTED_SOURCE_CONTEXT_INVALID")
        if (
            context.workspace_id != str(binding.workspace_id)
            or context.thread_id != str(binding.thread_id)
            or not _permission_hash_matches(
                context.permission_snapshot, binding.permission_snapshot_hash
            )
        ):
            return SummaryTrustDecision.denied("TRUSTED_SOURCE_PERMISSION_OR_SCOPE_CHANGED")
        return SummaryTrustDecision.verified(context)


class SummaryBudgetReserver(Protocol):
    """Reserve shared funds in the supplied transaction before any dispatch.

    A production implementation must lock and update its authoritative shared
    budget using this session. The reservation must roll back if this
    transaction fails and remain held for an unknown provider outcome.
    """

    async def reserve(
        self,
        session: AsyncSession,
        *,
        binding: SummaryJobBinding,
        reservation_id: UUID,
        amount: Decimal,
        currency: str,
    ) -> bool: ...


class DenyAllSummaryBudgetReserver:
    """A per-job price estimate alone does not reserve a shared budget."""

    async def reserve(
        self,
        session: AsyncSession,
        *,
        binding: SummaryJobBinding,
        reservation_id: UUID,
        amount: Decimal,
        currency: str,
    ) -> bool:
        del session, binding, reservation_id, amount, currency
        return False


class SummaryJobService:
    """Durable transition service for one summary job at a time."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        checkpoint_adapter: ExactCheckpointAdapter | None = None,
        verifier: SummaryBindingVerifier | None = None,
        budget_reserver: SummaryBudgetReserver | None = None,
        clock: Callable[[], datetime] | None = None,
        local_summary_experiment: LocalSummaryExperimentPermit | None = None,
        current_run_id: UUID | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_recovery_batch: int = DEFAULT_MAX_RECOVERY_BATCH,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("lease_seconds must be an integer")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if isinstance(max_recovery_batch, bool) or not isinstance(max_recovery_batch, int):
            raise TypeError("max_recovery_batch must be an integer")
        if not 1 <= max_recovery_batch <= 1_000:
            raise ValueError("max_recovery_batch must be between 1 and 1000")
        self._session_factory = session_factory
        self._checkpoint_adapter = checkpoint_adapter
        self._verifier = verifier or DenyAllSummaryBindingVerifier()
        self._budget_reserver = budget_reserver or DenyAllSummaryBudgetReserver()
        self._clock = clock or _utc_now
        if local_summary_experiment is not None and not isinstance(
            local_summary_experiment, LocalSummaryExperimentPermit
        ):
            raise TypeError("local_summary_experiment must be a local permit")
        if local_summary_experiment is not None and (
            current_run_id not in local_summary_experiment.run_ids
            or not local_summary_experiment.matches_session_factory(session_factory)
        ):
            raise ValueError("local summary experiment scope does not match this service")
        self._local_summary_experiment = local_summary_experiment
        self._current_run_id = current_run_id
        self._lease_seconds = lease_seconds
        self._max_recovery_batch = max_recovery_batch

    def _request_strategy_error(self, job: Any) -> SummaryServiceError | None:
        mode = _summary_request_mode(job)
        if mode == "provider_default":
            return None
        permit = getattr(self, "_local_summary_experiment", None)
        if (
            mode == SUMMARY_NON_THINKING_MODE
            and permit is not None
            and permit.authorizes_job(
                session_factory=self._session_factory,
                current_run_id=getattr(self, "_current_run_id", None),
                job=job,
            )
        ):
            return None
        if mode == SUMMARY_NON_THINKING_MODE:
            return SummaryServiceError(
                "SUMMARY_REQUEST_COUNTER_UNAVAILABLE",
                "The DeepSeek full-request counter is unavailable "
                "outside its bound local experiment.",
            )
        return SummaryServiceError(
            "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
            "The frozen summary request strategy is not supported.",
        )

    def _request_mode_search_allowed(self, request_mode: str) -> bool:
        if request_mode == "provider_default":
            return True
        permit = getattr(self, "_local_summary_experiment", None)
        return (
            request_mode == SUMMARY_NON_THINKING_MODE
            and permit is not None
            and getattr(self, "_current_run_id", None) in permit.run_ids
            and permit.matches_session_factory(self._session_factory)
        )

    def _verified_request_payload(
        self,
        job: ThreadSummaryJob,
        *,
        context: SummaryInputContext,
        input_tokens: int,
        output_tokens: int,
    ) -> dict[str, Any]:
        payload = _verified_request_spec(
            job,
            context=context,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        mode = _summary_request_mode(job)
        if mode != SUMMARY_NON_THINKING_MODE:
            return payload
        strategy_error = self._request_strategy_error(job)
        if strategy_error is not None:
            raise strategy_error
        if input_tokens > 32768:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_BUDGET_EXCEEDED",
                "The local experiment input window is limited to 32768 tokens.",
            )
        permit = self._local_summary_experiment
        assert permit is not None
        from packages.model_gateway.contracts import (
            AliyunDeepSeekRequestMode,
            ModelMessage,
            ModelRequest,
            RetryPolicy,
        )

        prompt = build_summary_prompt(context, prompt_version=job.prompt_version)
        request = ModelRequest(
            (
                ModelMessage("system", prompt.system_prompt),
                ModelMessage("user", prompt.user_prompt),
            ),
            retry_policy=RetryPolicy(max_attempts=1),
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        )
        try:
            actual_input_tokens = permit.counter.count_frozen_summary_request(
                request,
                request_identity=permit.request_identity,
                provider=permit.provider,
                model=permit.model,
            )
        except Exception as error:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_COUNTER_FAILED",
                "The frozen local experiment counter could not recount the dispatch request.",
            ) from error
        if type(actual_input_tokens) is not int or actual_input_tokens <= 0:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_COUNTER_INVALID",
                "The frozen local experiment counter returned an invalid count.",
            )
        if actual_input_tokens > input_tokens:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_BUDGET_EXCEEDED",
                "The full request exceeds the reserved model input budget.",
            )
        if output_tokens > permit.output_token_limit:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_BUDGET_EXCEEDED",
                "The requested output exceeds the local experiment output limit.",
            )
        payload["exact_input_tokens"] = actual_input_tokens
        payload["local_experiment_id"] = permit.experiment_id
        payload["experiment_request_identity"] = permit.request_hash(
            run_id=job.trigger_run_id,
            attempt_kind="SUMMARY",
            provider=permit.provider,
            model=permit.model,
            request=request,
            output_tokens=output_tokens,
            temperature=job.model_spec.get("temperature"),
        )
        return payload

    @staticmethod
    def _local_experiment_reservation(dispatch: ThreadSummaryDispatch) -> Mapping[str, Any] | None:
        reservation = (dispatch.budget_reservation or {}).get("local_experiment")
        return reservation if isinstance(reservation, Mapping) else None

    def _mark_local_experiment_unknown(
        self, dispatch: ThreadSummaryDispatch, *, reason: str
    ) -> None:
        permit = self._local_summary_experiment
        reservation = self._local_experiment_reservation(dispatch)
        if permit is None or reservation is None:
            return
        try:
            permit.mark_unknown(dict(reservation), reason=reason)
        except Exception:
            # The reserve already committed. Leaving it reserved is the safe
            # fallback if the outer ledger cannot record the uncertainty note.
            logger.warning("local_summary_experiment_unknown_note_failed", exc_info=True)

    def _settle_local_experiment_response(
        self,
        dispatch: ThreadSummaryDispatch,
        *,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        permit = self._local_summary_experiment
        reservation = self._local_experiment_reservation(dispatch)
        if permit is None or reservation is None:
            return
        from types import SimpleNamespace

        usage = (
            SimpleNamespace(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            if input_tokens is not None and output_tokens is not None and total_tokens is not None
            else None
        )
        try:
            if usage is None:
                permit.mark_unknown(dict(reservation), reason="provider usage is unavailable")
            else:
                permit.settle_response(
                    dict(reservation),
                    usage=usage,
                    provider=provider,
                    model=model,
                )
        except Exception:
            # Keep the research reservation at its full upper bound. The DB
            # response remains durable and can be reconciled without redispatch.
            logger.warning("local_summary_experiment_settlement_failed", exc_info=True)

    def _local_dispatch_integrity_error(
        self,
        job: ThreadSummaryJob,
        context: SummaryInputContext,
        dispatch: ThreadSummaryDispatch,
    ) -> str | None:
        try:
            payload = self._verified_request_payload(
                job,
                context=context,
                input_tokens=dispatch.reserved_input_tokens,
                output_tokens=dispatch.reserved_output_tokens,
            )
            digest = canonical_json_hash(
                {"version": SUMMARY_LEDGER_VERSION, "request": payload}
            )
        except SummaryServiceError as error:
            return error.code
        return None if digest == dispatch.request_hash else "SUMMARY_DISPATCH_PAYLOAD_MISMATCH"

    async def create_job(self, command: NewSummaryJob) -> CreatedSummaryJob:
        """Create an immutable job and source manifest in one bounded transaction."""

        strategy_error = self._request_strategy_error(command)
        if strategy_error is not None:
            raise strategy_error
        _validate_new_job(command)
        checkpoint = await self._verify_exact_checkpoint(command)
        model_round = _trusted_summary_model_round(command, checkpoint)
        identity_hash = _summary_identity_hash(
            command,
            checkpoint=checkpoint,
            model_round=model_round,
        )
        key = SummaryJobKey(command.workspace_id, command.thread_id, UUID(int=0))
        async with self._session_factory() as session:
            async with session.begin():
                run_thread = await session.scalar(
                    select(AgentRun.thread_id).where(
                        AgentRun.workspace_id == command.workspace_id,
                        AgentRun.id == command.trigger_run_id,
                    )
                )
                if run_thread != command.thread_id:
                    raise SummaryServiceError(
                        "SUMMARY_TRIGGER_SCOPE_INVALID",
                        "The trigger Run does not belong to the requested Thread.",
                    )

                legacy_job = await session.scalar(
                    select(ThreadSummaryJob.id)
                    .where(
                        ThreadSummaryJob.workspace_id == command.workspace_id,
                        ThreadSummaryJob.trigger_run_id == command.trigger_run_id,
                        or_(
                            ThreadSummaryJob.model_round.is_(None),
                            ThreadSummaryJob.checkpoint_id.is_(None),
                            ThreadSummaryJob.checkpoint_ns.is_(None),
                            ThreadSummaryJob.checkpoint_model_round_count.is_(None),
                            ThreadSummaryJob.checkpoint_binding_sha256.is_(None),
                        ),
                    )
                    .limit(1)
                )
                if legacy_job is not None:
                    raise SummaryServiceError(
                        "SUMMARY_LEGACY_BOUNDARY_UNVERIFIED",
                        "A legacy summary for this Run has no verifiable dispatch boundary.",
                    )

                existing = await session.scalar(
                    select(ThreadSummaryJob).where(
                        ThreadSummaryJob.workspace_id == command.workspace_id,
                        ThreadSummaryJob.thread_id == command.thread_id,
                        ThreadSummaryJob.identity_hash == identity_hash,
                    )
                )
                if existing is not None:
                    await self._verify_fixed_job_checkpoint(existing)
                    return CreatedSummaryJob(
                        key=SummaryJobKey(
                            existing.workspace_id,
                            existing.thread_id,
                            existing.id,
                        ),
                        identity_hash=existing.identity_hash,
                        status=existing.status,
                        created=False,
                    )

                boundary_owner = await session.scalar(
                    select(ThreadSummaryJob).where(
                        ThreadSummaryJob.workspace_id == command.workspace_id,
                        ThreadSummaryJob.trigger_run_id == command.trigger_run_id,
                        ThreadSummaryJob.model_round == model_round,
                    )
                )
                if boundary_owner is not None:
                    raise _boundary_conflict(boundary_owner)

                parent = await self._validate_parent(
                    session,
                    workspace_id=command.workspace_id,
                    thread_id=command.thread_id,
                    parent_artifact_id=command.parent_artifact_id,
                    parent_revision_hash=command.parent_revision_hash,
                )
                del parent
                job = ThreadSummaryJob(
                    id=uuid4(),
                    workspace_id=command.workspace_id,
                    thread_id=command.thread_id,
                    trigger_run_id=command.trigger_run_id,
                    model_round=model_round,
                    checkpoint_id=checkpoint.checkpoint_id,
                    checkpoint_ns=checkpoint.checkpoint_ns,
                    checkpoint_model_round_count=(checkpoint.checkpoint_model_round_count),
                    checkpoint_binding_sha256=checkpoint.checkpoint_binding_sha256,
                    identity_hash=identity_hash,
                    source_watermark=command.source_watermark,
                    source_scope=dict(command.source_scope),
                    source_scope_hash=canonical_json_hash(dict(command.source_scope)),
                    source_content_hash=_source_content_hash(command),
                    parent_artifact_id=command.parent_artifact_id,
                    parent_revision_hash=command.parent_revision_hash,
                    policy_spec=dict(command.policy_spec),
                    policy_version=command.policy_version,
                    prompt_version=command.prompt_version,
                    budget_version=command.budget_version,
                    model_spec=dict(command.model_spec),
                    model_spec_hash=canonical_json_hash(dict(command.model_spec)),
                    tokenizer_id=command.tokenizer_id,
                    tokenizer_version=command.tokenizer_version,
                    target_budget_tokens=command.target_budget_tokens,
                    permission_snapshot_hash=command.permission_snapshot_hash,
                    created_by=command.created_by,
                )
                try:
                    async with session.begin_nested():
                        session.add(job)
                        await session.flush()
                        for ordinal, source in enumerate(command.sources, start=1):
                            session.add(
                                ThreadSummaryJobSource(
                                    id=uuid4(),
                                    workspace_id=command.workspace_id,
                                    thread_id=command.thread_id,
                                    job_id=job.id,
                                    ordinal=ordinal,
                                    source_id=source.source_id,
                                    turn_id=source.turn_id,
                                    run_id=source.run_id,
                                    sequence=source.sequence,
                                    original_ref=source.original_ref,
                                    source_identity_hash=source.source_identity_hash,
                                    revision_sha256=source.revision_sha256,
                                    user_input_sha256=source.user_input_sha256,
                                    final_output_sha256=source.final_output_sha256,
                                    artifact_identities=[
                                        dict(item) for item in source.artifact_identities
                                    ],
                                    summary_eligibility=source.summary_eligibility,
                                    eligibility_reason=source.eligibility_reason,
                                )
                            )
                        await session.flush()
                except DBAPIError as error:
                    constraint_name = _constraint_name(error)
                    if constraint_name in (None, "uq_thread_summary_jobs_identity"):
                        existing = await session.scalar(
                            select(ThreadSummaryJob).where(
                                ThreadSummaryJob.workspace_id == command.workspace_id,
                                ThreadSummaryJob.thread_id == command.thread_id,
                                ThreadSummaryJob.identity_hash == identity_hash,
                            )
                        )
                        if existing is not None:
                            await self._verify_fixed_job_checkpoint(existing)
                            return CreatedSummaryJob(
                                key=SummaryJobKey(
                                    existing.workspace_id,
                                    existing.thread_id,
                                    existing.id,
                                ),
                                identity_hash=existing.identity_hash,
                                status=existing.status,
                                created=False,
                            )
                    if constraint_name in (
                        None,
                        "uq_thread_summary_jobs_dispatch_boundary",
                        "uq_thread_summary_jobs_exact_checkpoint",
                    ):
                        boundary_owner = await session.scalar(
                            select(ThreadSummaryJob).where(
                                ThreadSummaryJob.workspace_id == command.workspace_id,
                                ThreadSummaryJob.trigger_run_id == command.trigger_run_id,
                                ThreadSummaryJob.model_round == model_round,
                            )
                        )
                        if boundary_owner is not None:
                            raise _boundary_conflict(boundary_owner) from error
                    raise SummaryServiceError(
                        "SUMMARY_JOB_CREATE_REJECTED",
                        "The summary job or source manifest is invalid.",
                    ) from error

                key = SummaryJobKey(job.workspace_id, job.thread_id, job.id)
                return CreatedSummaryJob(
                    key=key,
                    identity_hash=identity_hash,
                    status=SummaryJobStatus.QUEUED.value,
                    created=True,
                )

    async def claim(
        self,
        key: SummaryJobKey,
        *,
        now: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> ClaimedSummaryJob | None:
        """Claim one queued job under its row lock."""

        duration = self._lease_duration(lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None or job.status != SummaryJobStatus.QUEUED.value:
                    return None
                token = secrets.token_hex(32)
                expiry = current + timedelta(seconds=duration)
                state = claim(_state(job), lease_token=token)
                _apply_state(job, state)
                job.lease_expires_at = expiry
                job.claimed_at = current
                job.failure_code = None
                await session.flush()
                return ClaimedSummaryJob(key=key, lease_token=token, lease_expires_at=expiry)

    async def claim_next(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        now: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> ClaimedSummaryJob | None:
        """Claim at most one queued job in an explicit tenant/thread scope."""

        duration = self._lease_duration(lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(ThreadSummaryJob)
                    .where(
                        ThreadSummaryJob.workspace_id == workspace_id,
                        ThreadSummaryJob.thread_id == thread_id,
                        ThreadSummaryJob.status == SummaryJobStatus.QUEUED.value,
                        ThreadSummaryJob.lease_token.is_(None),
                    )
                    .order_by(ThreadSummaryJob.created_at, ThreadSummaryJob.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                current = self._now(now)
                if job is None:
                    return None
                token = secrets.token_hex(32)
                expiry = current + timedelta(seconds=duration)
                _apply_state(job, claim(_state(job), lease_token=token))
                job.lease_expires_at = expiry
                job.claimed_at = current
                job.failure_code = None
                await session.flush()
                return ClaimedSummaryJob(
                    key=SummaryJobKey(job.workspace_id, job.thread_id, job.id),
                    lease_token=token,
                    lease_expires_at=expiry,
                )

    async def renew_lease(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        now: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> datetime:
        """Extend only a live owner of a pre-response state."""

        duration = self._lease_duration(lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                if job.status not in {
                    SummaryJobStatus.CLAIMED.value,
                    SummaryJobStatus.DISPATCH_INTENT.value,
                }:
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_NOT_RENEWABLE",
                        "Only pre-response job owners can renew a lease.",
                    )
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease is missing, expired, or owned elsewhere.",
                    )
                expiry = current + timedelta(seconds=duration)
                job.lease_expires_at = expiry
                await session.flush()
                return expiry

    async def release_pre_dispatch_claim(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> None:
        """Return a claimed job to the queue only before dispatch intent exists."""

        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                if job.status != SummaryJobStatus.CLAIMED.value:
                    raise SummaryServiceError(
                        "SUMMARY_RELEASE_REJECTED",
                        "Only a pre-dispatch claim can be released.",
                    )
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease is missing, expired, or owned elsewhere.",
                    )
                job.status = SummaryJobStatus.QUEUED.value
                job.lease_token = None
                job.lease_expires_at = None
                job.claimed_at = None
                await session.flush()

    async def fail_before_dispatch(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        failure_code: str,
        now: datetime | None = None,
    ) -> SummarySettlement:
        """Record a proven local failure before any dispatch intent exists."""

        if not isinstance(failure_code, str) or not failure_code.strip():
            raise ValueError("failure_code must be a non-empty string")
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                if job.status != SummaryJobStatus.CLAIMED.value:
                    raise SummaryServiceError(
                        "SUMMARY_FAILURE_REJECTED",
                        "A local failure may only follow a claim before dispatch intent.",
                    )
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease is missing, expired, or owned elsewhere.",
                    )
                state = fail_before_dispatch(_state(job), lease_token=lease_token)
                _apply_state(job, state)
                job.lease_token = None
                job.lease_expires_at = None
                job.failure_code = failure_code
                job.completed_at = current
                await session.flush()
                return _settlement(job)

    async def recover_expired_leases(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> tuple[RecoveredSummaryJob, ...]:
        """Recover a bounded set of expired rows under row locks.

        Claimed work returns to the queue.  An expired dispatch intent is
        ambiguous and becomes immutable ``UNKNOWN_OUTCOME``.  A persisted
        response loses only its expired owner and remains available for local
        validation recovery.
        """

        current = self._now(now)
        batch = self._recovery_limit(limit)
        recovered: list[RecoveredSummaryJob] = []
        async with self._session_factory() as session:
            async with session.begin():
                jobs = tuple(
                    await session.scalars(
                        select(ThreadSummaryJob)
                        .where(
                            ThreadSummaryJob.workspace_id == workspace_id,
                            ThreadSummaryJob.thread_id == thread_id,
                            ThreadSummaryJob.lease_token.is_not(None),
                            ThreadSummaryJob.lease_expires_at <= current,
                            ThreadSummaryJob.status.in_(
                                (
                                    SummaryJobStatus.CLAIMED.value,
                                    SummaryJobStatus.DISPATCH_INTENT.value,
                                    SummaryJobStatus.RESPONSE_STORED.value,
                                    SummaryJobStatus.VALIDATED.value,
                                )
                            ),
                        )
                        .order_by(ThreadSummaryJob.lease_expires_at, ThreadSummaryJob.id)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                )
                for job in jobs:
                    previous = job.status
                    state = recover_expired_lease(_state(job))
                    _apply_state(job, state)
                    job.lease_token = None
                    job.lease_expires_at = None
                    if state.status is SummaryJobStatus.UNKNOWN_OUTCOME:
                        job.failure_code = "SUMMARY_DISPATCH_OUTCOME_UNKNOWN"
                        job.completed_at = current
                        if job.dispatch_id is not None:
                            result = await session.execute(
                                update(ThreadSummaryDispatch)
                                .where(
                                    ThreadSummaryDispatch.workspace_id == workspace_id,
                                    ThreadSummaryDispatch.thread_id == thread_id,
                                    ThreadSummaryDispatch.job_id == job.id,
                                    ThreadSummaryDispatch.id == job.dispatch_id,
                                    ThreadSummaryDispatch.outcome.in_(
                                        (
                                            ThreadSummaryDispatchOutcome.INTENT_RECORDED.value,
                                            ThreadSummaryDispatchOutcome.DISPATCHED.value,
                                        )
                                    ),
                                )
                                .values(
                                    outcome=ThreadSummaryDispatchOutcome.UNKNOWN_OUTCOME.value,
                                    settled_at=current,
                                    updated_at=current,
                                )
                            )
                            if result.rowcount != 1:
                                raise SummaryServiceError(
                                    "SUMMARY_DISPATCH_INVARIANT",
                                    "The expired dispatch ledger could not be fenced.",
                                )
                    recovered.append(
                        RecoveredSummaryJob(
                            key=SummaryJobKey(job.workspace_id, job.thread_id, job.id),
                            previous_status=previous,
                            current_status=state.status.value,
                        )
                    )
                await session.flush()
        return tuple(recovered)

    async def claim_recovery(
        self,
        key: SummaryJobKey,
        *,
        now: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> ClaimedSummaryJob | None:
        """Acquire a fresh local-only lease after a response is durable."""

        duration = self._lease_duration(lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    return None
                if job.status not in {
                    SummaryJobStatus.RESPONSE_STORED.value,
                    SummaryJobStatus.VALIDATED.value,
                }:
                    return None
                if job.lease_token is not None and _is_live(job.lease_expires_at, current):
                    return None
                token = secrets.token_hex(32)
                expiry = current + timedelta(seconds=duration)
                if job.lease_token is not None:
                    job.lease_token = None
                    job.lease_expires_at = None
                _apply_state(job, claim_recovery(_state(job), lease_token=token))
                job.lease_expires_at = expiry
                await session.flush()
                return ClaimedSummaryJob(key=key, lease_token=token, lease_expires_at=expiry)

    async def prepare_dispatch(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        now: datetime | None = None,
    ) -> PreparedSummaryDispatch:
        """Reconstruct a trusted prompt without a provider call or reservation."""
        if not _positive_int(reserved_input_tokens) or not _positive_int(reserved_output_tokens):
            raise ValueError("summary reservation token bounds must be positive integers")
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND", "The summary job was not found."
                    )
                strategy_error = self._request_strategy_error(job)
                if strategy_error is not None:
                    raise strategy_error
                _require_model_round_boundary(job)
                if job.status != SummaryJobStatus.CLAIMED.value:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_STATE_INVALID", "Only a claimed job may be prepared."
                    )
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST", "The execution lease is unavailable."
                    )
                context = await self._trusted_dispatch_context(session, key, job)
                if not _owns_live_lease(job, lease_token, self._now(now)):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST", "The execution lease expired during preparation."
                    )
                prompt = build_summary_prompt(context, prompt_version=job.prompt_version)
                request_spec = self._verified_request_payload(
                    job,
                    context=context,
                    input_tokens=reserved_input_tokens,
                    output_tokens=reserved_output_tokens,
                )
                return PreparedSummaryDispatch(
                    key,
                    job.created_by,
                    job.policy_version,
                    job.prompt_version,
                    job.tokenizer_id,
                    job.tokenizer_version,
                    prompt,
                    request_spec,
                    dict(job.model_spec),
                )

    async def commit_dispatch_intent(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        pricing_snapshot_id: UUID,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        request_spec: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SummaryDispatchIntent:
        """Atomically create the one dispatch ledger row, reservation and pointer."""

        if not _positive_int(reserved_input_tokens):
            raise ValueError("reserved_input_tokens must be a positive integer")
        if not _positive_int(reserved_output_tokens):
            raise ValueError("reserved_output_tokens must be a positive integer")
        if not isinstance(pricing_snapshot_id, UUID):
            raise TypeError("pricing_snapshot_id must be a UUID")
        if (
            isinstance(request_spec, Mapping)
            and request_spec.get("request_mode") == SUMMARY_NON_THINKING_MODE
            and getattr(self, "_local_summary_experiment", None) is None
        ):
            raise SummaryServiceError(
                "SUMMARY_REQUEST_COUNTER_UNAVAILABLE",
                "The DeepSeek full-request counter is not registered for production dispatch.",
            )
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                strategy_error = self._request_strategy_error(job)
                if strategy_error is not None:
                    raise strategy_error
                _require_model_round_boundary(job)
                if job.status != SummaryJobStatus.CLAIMED.value:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_STATE_INVALID",
                        "Only a claimed pre-dispatch job may commit a dispatch intent.",
                    )
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease is missing, expired, or owned elsewhere.",
                    )
                if job.dispatch_id is not None or job.budget_reserved:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_ALREADY_INTENDED",
                        "A durable dispatch intent already exists.",
                    )
                existing_dispatch = await session.scalar(
                    select(ThreadSummaryDispatch.id)
                    .where(
                        ThreadSummaryDispatch.workspace_id == key.workspace_id,
                        ThreadSummaryDispatch.thread_id == key.thread_id,
                        ThreadSummaryDispatch.job_id == key.job_id,
                    )
                    .with_for_update()
                )
                if existing_dispatch is not None:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_ALREADY_INTENDED",
                        "A durable dispatch intent already exists.",
                    )

                context = await self._trusted_dispatch_context(session, key, job)

                pricing = await session.scalar(
                    select(PricingSnapshot)
                    .where(
                        PricingSnapshot.workspace_id == key.workspace_id,
                        PricingSnapshot.id == pricing_snapshot_id,
                    )
                    .with_for_update(read=True)
                )
                if pricing is None:
                    raise SummaryServiceError(
                        "SUMMARY_PRICING_NOT_FOUND",
                        "The pricing snapshot does not belong to the Workspace.",
                    )
                pricing_hash = _pricing_snapshot_content_hash(pricing)
                if pricing_hash != pricing.content_hash:
                    raise SummaryServiceError(
                        "SUMMARY_PRICING_INVALID",
                        "The pricing snapshot content hash is invalid.",
                    )
                provider = job.model_spec.get("provider")
                model = job.model_spec.get("model")
                if (
                    not isinstance(provider, str)
                    or not isinstance(model, str)
                    or provider != pricing.provider
                    or model != pricing.model
                ):
                    raise SummaryServiceError(
                        "SUMMARY_PRICING_MODEL_MISMATCH",
                        "The pricing snapshot does not match the frozen job model.",
                    )
                try:
                    cost = _reservation_cost(
                        pricing,
                        input_tokens=reserved_input_tokens,
                        output_tokens=reserved_output_tokens,
                    )
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise SummaryServiceError(
                        "SUMMARY_PRICING_INVALID",
                        "The pricing snapshot cannot produce a valid reservation.",
                    ) from error

                dispatch_id = uuid4()
                reservation_id = uuid4()
                request_payload = self._verified_request_payload(
                    job,
                    context=context,
                    input_tokens=reserved_input_tokens,
                    output_tokens=reserved_output_tokens,
                )
                if request_spec is not None and dict(request_spec) != request_payload:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_PAYLOAD_MISMATCH",
                        "The dispatch request does not match the verified source context.",
                    )
                try:
                    request_hash = canonical_json_hash(
                        {
                            "version": SUMMARY_LEDGER_VERSION,
                            "request": request_payload,
                        }
                    )
                    reservation_payload = {
                        "version": SUMMARY_LEDGER_VERSION,
                        "state": "RESERVED",
                        "job_id": str(job.id),
                        "identity_hash": job.identity_hash,
                        "pricing_snapshot_id": str(pricing.id),
                        "pricing_snapshot_hash": pricing_hash,
                        "input_tokens": reserved_input_tokens,
                        "output_tokens": reserved_output_tokens,
                        "cost_amount": str(cost),
                        "currency": pricing.currency,
                        "cost_is_estimate": True,
                    }
                    reservation_hash = canonical_json_hash(reservation_payload)
                    intent_hash = canonical_json_hash(
                        {
                            "version": SUMMARY_LEDGER_VERSION,
                            "job_id": str(job.id),
                            "identity_hash": job.identity_hash,
                            "request_hash": request_hash,
                            "reservation_hash": reservation_hash,
                        }
                    )
                except (TypeError, ValueError) as error:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_PAYLOAD_INVALID",
                        "The dispatch request is not canonical JSON.",
                    ) from error

                # This hook must reserve against the real shared budget using
                # the same transaction. A ledger row by itself is not a cap.
                reserved = await self._budget_reserver.reserve(
                    session,
                    binding=_binding(job, SummaryVerifierPhase.DISPATCH),
                    reservation_id=reservation_id,
                    amount=cost,
                    currency=pricing.currency,
                )
                if reserved is not True:
                    raise SummaryServiceError(
                        "SUMMARY_SHARED_BUDGET_UNAVAILABLE",
                        "A shared budget reservation is unavailable.",
                    )
                # Source verification and the shared-account lock can wait. A
                # lease that expires during either wait must roll back the
                # reservation rather than commit a stale dispatch intent.
                current = self._now(now)
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease expired while reserving dispatch.",
                    )
                # The fixed tuple and frozen history can change while source
                # verification and budget locking wait. Re-read the persisted
                # identity immediately before committing the one dispatch.
                await self._verify_fixed_job_checkpoint(job)
                current = self._now(now)
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease expired while rechecking the checkpoint.",
                    )

                if _summary_request_mode(job) == SUMMARY_NON_THINKING_MODE:
                    permit = self._local_summary_experiment
                    if permit is None:
                        raise SummaryServiceError(
                            "SUMMARY_REQUEST_COUNTER_UNAVAILABLE",
                            "The v2 dispatch has no bound local experiment ledger.",
                        )
                    exact_tokens = request_payload.get("exact_input_tokens")
                    request_identity = request_payload.get("experiment_request_identity")
                    if (
                        type(exact_tokens) is not int
                        or exact_tokens <= 0
                        or not isinstance(request_identity, str)
                        or not _sha256(request_identity)
                    ):
                        raise SummaryServiceError(
                            "SUMMARY_REQUEST_COUNTER_INVALID",
                            "The exact request count or ledger identity is unavailable.",
                        )
                    experiment_attempt_id = f"m4s-{job.id.hex}"
                    input_counter = {
                        "counter_id": permit.request_identity["request_counter_id"],
                        "tokenizer_revision": permit.request_identity[
                            "request_counter_revision"
                        ],
                        "thinking_mode": "NON_THINKING",
                        "tokens": exact_tokens,
                        "input_limit": min(reserved_input_tokens, 32768),
                        "request_identity": request_identity,
                    }
                    try:
                        outer_reservation = permit.reserve_dispatch(
                            attempt_id=experiment_attempt_id,
                            run_id=job.trigger_run_id,
                            attempt_kind="SUMMARY",
                            provider=provider,
                            model=model,
                            request_identity=request_identity,
                            estimated_input_tokens=32768,
                            requested_output_tokens=reserved_output_tokens,
                            maximum_cost_cny=cost,
                            currency=pricing.currency,
                            input_counter=input_counter,
                        )
                    except Exception as error:
                        raise SummaryServiceError(
                            "SUMMARY_EXPERIMENT_BUDGET_REJECTED",
                            "The shared research ledger refused the summary dispatch reservation.",
                        ) from error
                    outer_cost = getattr(outer_reservation, "reserved_cost_cny", None)
                    if not isinstance(outer_cost, Decimal) or not outer_cost.is_finite():
                        raise SummaryServiceError(
                            "SUMMARY_EXPERIMENT_BUDGET_INVALID",
                            "The shared research ledger returned no valid reservation.",
                        )
                    reservation_payload["local_experiment"] = {
                        "experiment_id": permit.experiment_id,
                        "attempt_id": experiment_attempt_id,
                        "request_identity": request_identity,
                        "reserved_cost_cny": str(outer_cost),
                    }
                    reservation_hash = canonical_json_hash(reservation_payload)
                    intent_hash = canonical_json_hash(
                        {
                            "version": SUMMARY_LEDGER_VERSION,
                            "job_id": str(job.id),
                            "identity_hash": job.identity_hash,
                            "request_hash": request_hash,
                            "reservation_hash": reservation_hash,
                        }
                    )

                dispatch = ThreadSummaryDispatch(
                    id=dispatch_id,
                    workspace_id=key.workspace_id,
                    thread_id=key.thread_id,
                    job_id=key.job_id,
                    attempt_number=1,
                    request_hash=request_hash,
                    intent_hash=intent_hash,
                    reservation_id=reservation_id,
                    pricing_snapshot_id=pricing.id,
                    reserved_input_tokens=reserved_input_tokens,
                    reserved_output_tokens=reserved_output_tokens,
                    reserved_cost_amount=cost,
                    reserved_currency=pricing.currency,
                    cost_is_estimate=True,
                    budget_reservation=reservation_payload,
                    budget_reservation_hash=reservation_hash,
                )
                session.add(dispatch)
                await session.flush()
                _apply_state(
                    job,
                    commit_dispatch_intent(_state(job), lease_token=lease_token),
                )
                job.dispatch_id = dispatch.id
                job.dispatch_intent_at = current
                await session.flush()
                if not _owns_live_lease(job, lease_token, self._now(now)):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease expired before the dispatch intent committed.",
                    )
                return SummaryDispatchIntent(
                    key=key,
                    dispatch_id=dispatch.id,
                    reservation_id=dispatch.reservation_id,
                    request_hash=request_hash,
                    intent_hash=intent_hash,
                    pricing_snapshot_id=pricing.id,
                    reserved_input_tokens=reserved_input_tokens,
                    reserved_output_tokens=reserved_output_tokens,
                    reserved_cost_amount=cost,
                    reserved_currency=pricing.currency,
                )

    async def mark_dispatched(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> None:
        """Fence the provider boundary immediately before the adapter call."""

        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                _require_model_round_boundary(job)
                if job.status != SummaryJobStatus.DISPATCH_INTENT.value:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_STATE_INVALID",
                        "Only an outstanding dispatch intent may be marked dispatched.",
                    )
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease is missing, expired, or owned elsewhere.",
                    )
                dispatch = await self._lock_dispatch(session, key, job.dispatch_id)
                if dispatch is None:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_INVARIANT",
                        "The dispatch intent pointer is missing.",
                    )
                if dispatch.outcome == ThreadSummaryDispatchOutcome.DISPATCHED.value:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_ALREADY_STARTED",
                        "The dispatch boundary was already crossed and cannot be repeated.",
                    )
                if dispatch.outcome != ThreadSummaryDispatchOutcome.INTENT_RECORDED.value:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_STATE_INVALID",
                        "The dispatch ledger is already terminal.",
                    )
                context = await self._trusted_dispatch_context(session, key, job)
                request_payload = self._verified_request_payload(
                    job,
                    context=context,
                    input_tokens=dispatch.reserved_input_tokens,
                    output_tokens=dispatch.reserved_output_tokens,
                )
                current_request_hash = canonical_json_hash(
                    {"version": SUMMARY_LEDGER_VERSION, "request": request_payload}
                )
                if current_request_hash != dispatch.request_hash:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_PAYLOAD_MISMATCH",
                        "The verified request changed after the dispatch intent was committed.",
                    )
                current = self._now(now)
                if not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease expired while validating dispatch.",
                    )
                result = await session.execute(
                    update(ThreadSummaryDispatch)
                    .where(
                        ThreadSummaryDispatch.workspace_id == key.workspace_id,
                        ThreadSummaryDispatch.thread_id == key.thread_id,
                        ThreadSummaryDispatch.job_id == key.job_id,
                        ThreadSummaryDispatch.id == dispatch.id,
                        ThreadSummaryDispatch.outcome
                        == ThreadSummaryDispatchOutcome.INTENT_RECORDED.value,
                    )
                    .values(
                        outcome=ThreadSummaryDispatchOutcome.DISPATCHED.value,
                        dispatched_at=current,
                        updated_at=current,
                    )
                )
                if result.rowcount != 1:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_STATE_INVALID",
                        "The dispatch ledger changed concurrently.",
                    )
                if not _owns_live_lease(job, lease_token, self._now(now)):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The execution lease expired before the dispatch fence committed.",
                    )

    async def mark_dispatch_unknown(
        self, key: SummaryJobKey, *, lease_token: str, now: datetime | None = None
    ) -> SummarySettlement:
        """Fence an unconfirmed provider outcome without waiting for lease expiry."""
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND", "The summary job was not found."
                    )
                if job.status in {
                    SummaryJobStatus.UNKNOWN_OUTCOME.value,
                    SummaryJobStatus.RESPONSE_STORED.value,
                    SummaryJobStatus.VALIDATED.value,
                    SummaryJobStatus.PUBLISHED.value,
                }:
                    if job.status == SummaryJobStatus.UNKNOWN_OUTCOME.value:
                        dispatch = await self._lock_dispatch(session, key, job.dispatch_id)
                        if dispatch is not None:
                            self._mark_local_experiment_unknown(
                                dispatch, reason="dispatch outcome remains unknown"
                            )
                    return _settlement(job)
                if job.status != SummaryJobStatus.DISPATCH_INTENT.value:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_STATE_INVALID", "No outstanding dispatch can be fenced."
                    )
                if (
                    not isinstance(lease_token, str)
                    or not lease_token
                    or job.lease_token != lease_token
                ):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST", "The dispatch lease is unavailable."
                    )
                dispatch = await self._lock_dispatch(session, key, job.dispatch_id)
                if dispatch is None:
                    raise SummaryServiceError(
                        "SUMMARY_DISPATCH_INVARIANT", "The dispatch intent pointer is missing."
                    )
                _mark_unknown(job, current)
                await self._mark_dispatch_unknown(session, key, dispatch, current)
                self._mark_local_experiment_unknown(
                    dispatch, reason="provider dispatch outcome is unknown"
                )
                await session.flush()
                return _settlement(job)

    async def store_provider_response(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        response: SummaryResponseData,
        now: datetime | None = None,
    ) -> StoredSummaryResponse:
        """Persist provider evidence before parsing or validating its content."""

        _validate_response_data(response)
        try:
            raw_response, response_hash = _encode_raw_response(response.raw_response)
            if (
                len(
                    json.dumps(
                        raw_response,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                > MAX_RESPONSE_BYTES
            ):
                raise ValueError("response exceeds byte limit")
            canonical_json_hash(dict(response.usage_metadata))
        except (TypeError, ValueError, RecursionError) as error:
            raise SummaryServiceError(
                "SUMMARY_RESPONSE_INVALID",
                "The provider response cannot be stored as bounded data.",
            ) from error
        expired = False
        terminal = False
        stored: StoredSummaryResponse | None = None
        experiment_dispatch: ThreadSummaryDispatch | None = None
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                if job.status == SummaryJobStatus.UNKNOWN_OUTCOME.value:
                    terminal = True
                elif job.status != SummaryJobStatus.DISPATCH_INTENT.value:
                    raise SummaryServiceError(
                        "SUMMARY_RESPONSE_STATE_INVALID",
                        "A response may only follow an outstanding dispatch intent.",
                    )
                elif not _owns_live_lease(job, lease_token, current):
                    if job.lease_token is not None and _is_live(job.lease_expires_at, current):
                        raise SummaryServiceError(
                            "SUMMARY_LEASE_LOST",
                            "The response lease is missing, expired, or owned elsewhere.",
                        )
                    expired = True
                dispatch = None
                if not terminal:
                    dispatch = await self._lock_dispatch(session, key, job.dispatch_id)
                    if dispatch is None:
                        raise SummaryServiceError(
                            "SUMMARY_DISPATCH_INVARIANT",
                            "The dispatch intent pointer is missing.",
                        )
                    # Acquiring the dispatch row can itself wait for another
                    # transaction. Check the lease again after that wait.
                    current = self._now(now)
                    if not _owns_live_lease(job, lease_token, current):
                        if job.lease_token is not None and _is_live(job.lease_expires_at, current):
                            raise SummaryServiceError(
                                "SUMMARY_LEASE_LOST", "The response lease belongs to another owner."
                            )
                        expired = True
                if expired:
                    _mark_unknown(job, current)
                    assert dispatch is not None
                    await self._mark_dispatch_unknown(session, key, dispatch, current)
                    self._mark_local_experiment_unknown(
                        dispatch, reason="response lease expired before persistence"
                    )
                elif not terminal:
                    assert dispatch is not None
                    if dispatch.outcome != ThreadSummaryDispatchOutcome.DISPATCHED.value:
                        raise SummaryServiceError(
                            "SUMMARY_DISPATCH_NOT_STARTED",
                            "A response requires the verified dispatch boundary to be crossed.",
                        )
                    response_id = uuid4()
                    session.add(
                        ThreadSummaryResponse(
                            id=response_id,
                            workspace_id=key.workspace_id,
                            thread_id=key.thread_id,
                            job_id=key.job_id,
                            dispatch_id=dispatch.id,
                            provider=response.provider,
                            model=response.model,
                            response_sha256=response_hash,
                            raw_response=raw_response,
                            usage_metadata=dict(response.usage_metadata),
                            input_tokens=response.input_tokens,
                            output_tokens=response.output_tokens,
                            total_tokens=response.total_tokens,
                            cached_tokens=response.cached_tokens,
                            cost_amount=response.cost_amount,
                            cost_currency=response.cost_currency,
                            cost_is_estimate=response.cost_is_estimate,
                            finish_reason=response.finish_reason,
                            stored_at=current,
                        )
                    )
                    await session.flush()
                    if not _owns_live_lease(job, lease_token, self._now(now)):
                        raise SummaryServiceError(
                            "SUMMARY_LEASE_LOST",
                            "The response lease expired while saving provider evidence.",
                        )
                    state = store_response(
                        _state(job),
                        lease_token=lease_token,
                        response_ref=str(response_id),
                    )
                    _apply_state(job, state)
                    job.response_id = response_id
                    job.response_stored_at = current
                    job.lease_token = None
                    job.lease_expires_at = None
                    result = await session.execute(
                        update(ThreadSummaryDispatch)
                        .where(
                            ThreadSummaryDispatch.workspace_id == key.workspace_id,
                            ThreadSummaryDispatch.thread_id == key.thread_id,
                            ThreadSummaryDispatch.job_id == key.job_id,
                            ThreadSummaryDispatch.id == dispatch.id,
                            ThreadSummaryDispatch.outcome
                            == ThreadSummaryDispatchOutcome.DISPATCHED.value,
                        )
                        .values(
                            outcome=ThreadSummaryDispatchOutcome.RESPONSE_RECEIVED.value,
                            settled_at=current,
                            updated_at=current,
                        )
                    )
                    if result.rowcount != 1:
                        raise SummaryServiceError(
                            "SUMMARY_DISPATCH_INVARIANT",
                            "The dispatch ledger could not record the response.",
                        )
                    await session.flush()
                    stored = StoredSummaryResponse(
                        key=key,
                        response_id=response_id,
                        response_sha256=response_hash,
                        status=SummaryJobStatus.RESPONSE_STORED.value,
                    )
                    experiment_dispatch = dispatch
        if terminal:
            raise SummaryServiceError(
                "SUMMARY_UNKNOWN_OUTCOME_TERMINAL",
                "An unknown dispatch outcome cannot accept a late response.",
            )
        if expired:
            raise SummaryServiceError(
                "SUMMARY_LEASE_EXPIRED_UNKNOWN",
                "The expired dispatch intent was recorded as UNKNOWN_OUTCOME.",
            )
        assert stored is not None
        if experiment_dispatch is not None:
            self._settle_local_experiment_response(
                experiment_dispatch,
                provider=response.provider,
                model=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                total_tokens=response.total_tokens,
            )
        return stored

    async def validate_stored_response(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        now: datetime | None = None,
    ) -> SummarySettlement:
        """Validate a stored response and persist one immutable pending artifact."""

        trusted_error: SummaryServiceError | None = None
        settlement: SummarySettlement | None = None
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                if job.status == SummaryJobStatus.UNKNOWN_OUTCOME.value:
                    raise SummaryServiceError(
                        "SUMMARY_UNKNOWN_OUTCOME_TERMINAL",
                        "An unknown dispatch outcome is immutable.",
                    )
                if job.status in {
                    SummaryJobStatus.VALIDATED.value,
                    SummaryJobStatus.PUBLISHED.value,
                    SummaryJobStatus.STALE.value,
                    SummaryJobStatus.FAILED.value,
                }:
                    if job.status in {
                        SummaryJobStatus.VALIDATED.value,
                        SummaryJobStatus.PUBLISHED.value,
                    }:
                        strategy_error = self._request_strategy_error(job)
                        if strategy_error is not None:
                            raise strategy_error
                    settlement = _settlement(job)
                    trusted_error = None
                elif job.status != SummaryJobStatus.RESPONSE_STORED.value:
                    raise SummaryServiceError(
                        "SUMMARY_VALIDATION_STATE_INVALID",
                        "Only a durable response may be validated.",
                    )
                elif not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The validation lease is missing, expired, or owned elsewhere.",
                    )
                else:
                    await self._verify_fixed_job_checkpoint(job)
                    stored_sources = await self._load_sources(session, key)
                    _verify_job_source_scope(
                        key,
                        job,
                        sources=stored_sources,
                    )
                    response = await self._lock_response(session, key, job.response_id)
                    if response is None:
                        raise SummaryServiceError(
                            "SUMMARY_RESPONSE_INVARIANT",
                            "The stored response pointer is missing.",
                        )
                    dispatch = await self._lock_dispatch(session, key, job.dispatch_id)
                    if (
                        dispatch is None
                        or dispatch.id != response.dispatch_id
                        or dispatch.outcome != ThreadSummaryDispatchOutcome.RESPONSE_RECEIVED.value
                    ):
                        raise SummaryServiceError(
                            "SUMMARY_DISPATCH_INVARIANT",
                            "The stored response has no matching completed dispatch.",
                        )
                    bound_failure = _response_bound_failure(response, dispatch)
                    sources = stored_sources
                    if not sources:
                        raise SummaryServiceError(
                            "SUMMARY_SOURCE_INVARIANT",
                            "The immutable source manifest is empty.",
                        )
                    self._settle_local_experiment_response(
                        dispatch,
                        provider=response.provider,
                        model=response.model,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        total_tokens=response.total_tokens,
                    )
                    binding = _binding(job, SummaryVerifierPhase.RESPONSE_VALIDATION)
                    decision = await self._verify(session, binding)
                    current = self._now(now)
                    if not _owns_live_lease(job, lease_token, current):
                        raise SummaryServiceError(
                            "SUMMARY_LEASE_LOST", "The validation lease expired during review."
                        )
                    if decision.status is SummaryTrustStatus.UNAVAILABLE:
                        trusted_error = SummaryServiceError(
                            "SUMMARY_TRUST_UNAVAILABLE",
                            "Trusted source and permission verification is unavailable.",
                        )
                    elif decision.status is SummaryTrustStatus.DENIED:
                        _mark_stale(job, current, _failure_code(decision, "SUMMARY_BINDING_STALE"))
                        settlement = _settlement(job)
                    else:
                        context = decision.validation_context
                        if context is None:
                            trusted_error = SummaryServiceError(
                                "SUMMARY_TRUST_CONTEXT_MISSING",
                                "Trusted verification did not return validation context.",
                            )
                        elif not _context_matches_job(context, job, sources):
                            _mark_stale(job, current, "SUMMARY_TRUST_CONTEXT_MISMATCH")
                            settlement = _settlement(job)
                        elif response.provider != job.model_spec.get(
                            "provider"
                        ) or response.model != job.model_spec.get("model"):
                            state = validate_response(
                                _state(job), lease_token=lease_token, valid=False
                            )
                            _apply_state(job, state)
                            job.validation_result = {
                                "valid": False,
                                "issues": [{"code": "RESPONSE_MODEL_MISMATCH"}],
                            }
                            job.failure_code = "SUMMARY_RESPONSE_MODEL_MISMATCH"
                            job.lease_token = None
                            job.lease_expires_at = None
                            job.completed_at = current
                            settlement = _settlement(job)
                        elif response.finish_reason != "stop" or bound_failure is not None:
                            state = validate_response(
                                _state(job), lease_token=lease_token, valid=False
                            )
                            _apply_state(job, state)
                            issues = []
                            if response.finish_reason != "stop":
                                issues.append({"code": "INCOMPLETE_PROVIDER_RESPONSE"})
                            if bound_failure is not None:
                                issues.append({"code": bound_failure})
                            job.validation_result = {
                                "valid": False,
                                "issues": issues,
                            }
                            job.failure_code = (
                                "SUMMARY_PROVIDER_INCOMPLETE"
                                if response.finish_reason != "stop"
                                else bound_failure
                            )
                            job.lease_token = None
                            job.lease_expires_at = None
                            job.completed_at = current
                            settlement = _settlement(job)
                        elif (strategy_error := self._request_strategy_error(job)) is not None:
                            state = validate_response(
                                _state(job), lease_token=lease_token, valid=False
                            )
                            _apply_state(job, state)
                            job.validation_result = {
                                "valid": False,
                                "issues": [{"code": strategy_error.code}],
                            }
                            job.failure_code = strategy_error.code
                            job.lease_token = None
                            job.lease_expires_at = None
                            job.completed_at = current
                            settlement = _settlement(job)
                        elif (
                            _summary_request_mode(job) == SUMMARY_NON_THINKING_MODE
                            and (
                                request_error := self._local_dispatch_integrity_error(
                                    job, context, dispatch
                                )
                            ) is not None
                        ):
                            state = validate_response(
                                _state(job), lease_token=lease_token, valid=False
                            )
                            _apply_state(job, state)
                            job.validation_result = {
                                "valid": False,
                                "issues": [{"code": request_error}],
                            }
                            job.failure_code = request_error
                            job.lease_token = None
                            job.lease_expires_at = None
                            job.completed_at = current
                            settlement = _settlement(job)
                        elif (
                            job.tokenizer_id != "approx_language_v1" or job.tokenizer_version != "1"
                        ):
                            trusted_error = SummaryServiceError(
                                "SUMMARY_COUNTER_UNSUPPORTED",
                                "The frozen summary token counter is unavailable.",
                            )
                        else:
                            validation = validate_summary_candidate(
                                _decode_raw_response(response),
                                context=context,
                                counter=_SummaryCounter(),
                            )
                            if not validation.valid:
                                state = validate_response(
                                    _state(job),
                                    lease_token=lease_token,
                                    valid=False,
                                )
                                _apply_state(job, state)
                                job.validation_result = validation.as_dict()
                                job.failure_code = "SUMMARY_VALIDATION_FAILED"
                                job.lease_token = None
                                job.lease_expires_at = None
                                job.completed_at = current
                                settlement = _settlement(job, validation=validation)
                            else:
                                artifact_error = await self._persist_validated_artifact(
                                    session,
                                    key,
                                    job,
                                    response,
                                    sources,
                                    validation,
                                    lease_token=lease_token,
                                    now=current,
                                    clock_override=now,
                                )
                                if artifact_error is None:
                                    settlement = _settlement(job, validation=validation)
                                else:
                                    trusted_error = artifact_error
                await session.flush()
        if trusted_error is not None:
            raise trusted_error
        if settlement is None:
            raise SummaryServiceError(
                "SUMMARY_VALIDATION_INVARIANT",
                "Validation did not produce a durable settlement.",
            )
        return settlement

    async def publish(
        self,
        key: SummaryJobKey,
        *,
        lease_token: str,
        published_by: str,
        now: datetime | None = None,
    ) -> SummarySettlement:
        """Revalidate trusted bindings and CAS-publish a pending artifact."""

        if not isinstance(published_by, str) or not published_by.strip():
            raise ValueError("published_by must be a non-empty string")
        unavailable_code: str | None = None
        settlement: SummarySettlement | None = None
        async with self._session_factory() as session:
            async with session.begin():
                job = await self._lock_job(session, key)
                current = self._now(now)
                if job is None:
                    raise SummaryServiceError(
                        "SUMMARY_JOB_NOT_FOUND",
                        "The summary job was not found.",
                    )
                if job.status == SummaryJobStatus.UNKNOWN_OUTCOME.value:
                    raise SummaryServiceError(
                        "SUMMARY_UNKNOWN_OUTCOME_TERMINAL",
                        "An unknown dispatch outcome is immutable.",
                    )
                if job.status == SummaryJobStatus.STALE.value:
                    raise SummaryServiceError(
                        job.failure_code or "SUMMARY_BINDING_STALE",
                        "The summary binding is no longer valid.",
                    )
                if job.status == SummaryJobStatus.PUBLISHED.value:
                    strategy_error = self._request_strategy_error(job)
                    if strategy_error is not None:
                        raise strategy_error
                    settlement = _settlement(job)
                elif job.status != SummaryJobStatus.VALIDATED.value:
                    raise SummaryServiceError(
                        "SUMMARY_PUBLISH_STATE_INVALID",
                        "Only a validated job may be published.",
                    )
                elif not _owns_live_lease(job, lease_token, current):
                    raise SummaryServiceError(
                        "SUMMARY_LEASE_LOST",
                        "The publication lease is missing, expired, or owned elsewhere.",
                    )
                else:
                    strategy_error = self._request_strategy_error(job)
                    if strategy_error is not None:
                        raise strategy_error
                    await self._verify_fixed_job_checkpoint(job)
                    await self._lock_summary_thread(session, key)
                    artifact = await self._lock_artifact(session, key, job.artifact_id)
                    if artifact is None:
                        raise SummaryServiceError(
                            "SUMMARY_ARTIFACT_INVARIANT",
                            "The validated artifact pointer is missing.",
                        )
                    if artifact.publication_status != ThreadSummaryArtifactStatus.PENDING.value:
                        raise SummaryServiceError(
                            "SUMMARY_ARTIFACT_INVARIANT",
                            "The artifact is no longer pending.",
                        )
                    parent_valid, parent_code = await self._parent_is_current(session, job)
                    binding = _binding(job, SummaryVerifierPhase.PUBLICATION)
                    decision = await self._verify(session, binding)
                    current = self._now(now)
                    if not _owns_live_lease(job, lease_token, current):
                        raise SummaryServiceError(
                            "SUMMARY_LEASE_LOST", "The publication lease expired during review."
                        )
                    if not parent_valid:
                        _mark_stale(job, current, parent_code)
                        await self._mark_artifact_stale(session, artifact)
                        settlement = _settlement(job)
                    elif decision.status is SummaryTrustStatus.DENIED:
                        code = _failure_code(decision, "SUMMARY_BINDING_STALE")
                        _mark_stale(job, current, code)
                        await self._mark_artifact_stale(session, artifact)
                        settlement = _settlement(job)
                    elif decision.status is SummaryTrustStatus.UNAVAILABLE:
                        job.failure_code = _failure_code(
                            decision,
                            "SUMMARY_PUBLICATION_VERIFIER_UNAVAILABLE",
                        )
                        unavailable_code = job.failure_code
                    else:
                        lease_expires_at = job.lease_expires_at
                        artifact_result = await session.execute(
                            update(ThreadSummaryArtifact)
                            .where(
                                ThreadSummaryArtifact.workspace_id == key.workspace_id,
                                ThreadSummaryArtifact.thread_id == key.thread_id,
                                ThreadSummaryArtifact.job_id == key.job_id,
                                ThreadSummaryArtifact.id == artifact.id,
                                ThreadSummaryArtifact.publication_status
                                == ThreadSummaryArtifactStatus.PENDING.value,
                            )
                            .values(
                                publication_status=ThreadSummaryArtifactStatus.PUBLISHED.value,
                                published_at=current,
                                published_by=published_by,
                            )
                        )
                        if artifact_result.rowcount != 1:
                            raise SummaryServiceError(
                                "SUMMARY_ARTIFACT_CAS_FAILED",
                                "The artifact publication compare-and-set failed.",
                            )
                        new_state = publish(
                            _state(job),
                            lease_token=lease_token,
                            revision_ref=str(artifact.id),
                            bindings_current=True,
                        )
                        job_result = await session.execute(
                            update(ThreadSummaryJob)
                            .where(
                                ThreadSummaryJob.workspace_id == key.workspace_id,
                                ThreadSummaryJob.thread_id == key.thread_id,
                                ThreadSummaryJob.id == key.job_id,
                                ThreadSummaryJob.status == SummaryJobStatus.VALIDATED.value,
                                ThreadSummaryJob.lease_token == lease_token,
                                ThreadSummaryJob.artifact_id == artifact.id,
                            )
                            .values(
                                status=new_state.status.value,
                                lease_token=None,
                                lease_expires_at=None,
                                published_at=current,
                                completed_at=current,
                                failure_code=None,
                            )
                        )
                        if job_result.rowcount != 1:
                            raise SummaryServiceError(
                                "SUMMARY_JOB_CAS_FAILED",
                                "The job publication compare-and-set failed.",
                            )
                        if not _is_live(lease_expires_at, self._now(now)):
                            raise SummaryServiceError(
                                "SUMMARY_LEASE_LOST",
                                "The publication lease expired before the update completed.",
                            )
                        job.status = SummaryJobStatus.PUBLISHED.value
                        job.lease_token = None
                        job.lease_expires_at = None
                        job.published_at = current
                        job.completed_at = current
                        job.failure_code = None
                        settlement = SummarySettlement(
                            key=key,
                            status=SummaryJobStatus.PUBLISHED.value,
                            response_id=job.response_id,
                            artifact_id=job.artifact_id,
                            validation_result=job.validation_result,
                        )
                await session.flush()
        if unavailable_code is not None:
            raise SummaryServiceError(
                unavailable_code,
                "Trusted publication verification is unavailable; the artifact remains pending.",
            )
        assert settlement is not None
        return settlement

    async def find_verified_summary_prefix(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        sources: Sequence[NewSummarySource],
        policy_version: str,
        prompt_version: str,
        tokenizer_id: str,
        tokenizer_version: str,
        model_spec: Mapping[str, Any],
        permission_snapshot_hash: str,
        source_scope: Mapping[str, Any],
        request_mode: str = "provider_default",
    ) -> PublishedSummaryRevision | None:
        """Find the longest published prefix matching freshly replayed sources."""
        candidates = tuple(sources)
        if not candidates or not self._request_mode_search_allowed(request_mode):
            return None
        if any(
            source.summary_eligibility != "ELIGIBLE"
            or source.eligibility_reason != PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
            for source in candidates
        ):
            return None
        try:
            model_spec_hash = canonical_json_hash(dict(model_spec))
        except (TypeError, ValueError, RecursionError):
            return None

        matches: list[PublishedSummaryRevision] = []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ThreadSummaryArtifact,
                        ThreadSummaryJob,
                        ThreadSummaryResponse,
                    )
                    .join(
                        ThreadSummaryJob,
                        (ThreadSummaryJob.workspace_id == ThreadSummaryArtifact.workspace_id)
                        & (ThreadSummaryJob.thread_id == ThreadSummaryArtifact.thread_id)
                        & (ThreadSummaryJob.id == ThreadSummaryArtifact.job_id),
                    )
                    .join(
                        ThreadSummaryResponse,
                        (ThreadSummaryResponse.workspace_id == ThreadSummaryArtifact.workspace_id)
                        & (ThreadSummaryResponse.thread_id == ThreadSummaryArtifact.thread_id)
                        & (ThreadSummaryResponse.job_id == ThreadSummaryArtifact.job_id)
                        & (ThreadSummaryResponse.id == ThreadSummaryArtifact.response_id),
                    )
                    .where(
                        ThreadSummaryArtifact.workspace_id == workspace_id,
                        ThreadSummaryArtifact.thread_id == thread_id,
                        ThreadSummaryArtifact.publication_status
                        == ThreadSummaryArtifactStatus.PUBLISHED.value,
                        ThreadSummaryJob.status == SummaryJobStatus.PUBLISHED.value,
                        ThreadSummaryJob.policy_version == policy_version,
                        ThreadSummaryJob.prompt_version == prompt_version,
                        ThreadSummaryJob.tokenizer_id == tokenizer_id,
                        ThreadSummaryJob.tokenizer_version == tokenizer_version,
                        ThreadSummaryJob.model_spec_hash == model_spec_hash,
                        ThreadSummaryJob.permission_snapshot_hash == permission_snapshot_hash,
                    )
                    .order_by(
                        ThreadSummaryArtifact.revision_number.desc(),
                        ThreadSummaryArtifact.id.desc(),
                    )
                    .limit(128)
                )
            ).all()
            for artifact, job, response in rows:
                if (
                    not _source_scopes_compatible(job.source_scope, source_scope)
                    or _summary_request_mode(job) != request_mode
                    or self._request_strategy_error(job) is not None
                ):
                    continue
                key = SummaryJobKey(workspace_id, thread_id, job.id)
                source_rows = await self._load_sources(session, key)
                if not _artifact_is_integrity_valid(artifact, job, response, source_rows):
                    continue
                index = tuple(artifact.source_index)
                if not _source_index_matches_prefix(index, candidates):
                    continue
                validation = dict(artifact.validation_result)
                covered = validation.get("covered_source_ids")
                expected_ids = [item["source_id"] for item in index]
                if covered != expected_ids or artifact.summary_content.get("omitted_source_ids"):
                    continue
                matches.append(
                    PublishedSummaryRevision(
                        key=key,
                        artifact_id=artifact.id,
                        response_id=response.id,
                        revision_number=artifact.revision_number,
                        revision_hash=artifact.revision_hash,
                        content_hash=artifact.content_hash,
                        summary_content=dict(artifact.summary_content),
                        source_index=tuple(dict(item) for item in index),
                        validation_result=validation,
                        policy_version=job.policy_version,
                        prompt_version=job.prompt_version,
                        source_scope=dict(job.source_scope),
                    )
                )
        ordered_matches = sorted(
            matches,
            key=lambda item: (len(item.source_index), item.revision_number, str(item.artifact_id)),
            reverse=True,
        )
        permission_or_lease_rejections = {
            "RUN_EXECUTION_LEASE_LOST",
            "RUN_ACTOR_ACCESS_REVOKED",
            "SUMMARY_CURRENT_PERMISSION_CHANGED",
            "SUMMARY_TOOL_HISTORY_PERMISSION_REVOKED",
            "SUMMARY_ARTIFACT_PERMISSION_REVOKED",
            "SUMMARY_EVIDENCE_PERMISSION_REVOKED",
        }
        for candidate in ordered_matches:
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        # Match the dispatch/recovery lock order: summary job,
                        # then the verifier's current Run, permission and
                        # frozen source rows. Provider replay stays read-only
                        # in its own session and this transaction ends before
                        # the caller can issue a model request.
                        job = await self._lock_job(session, candidate.key)
                        if (
                            job is None
                            or job.status != SummaryJobStatus.PUBLISHED.value
                            or job.artifact_id != candidate.artifact_id
                            or self._request_strategy_error(job) is not None
                        ):
                            continue
                        artifact = await session.scalar(
                            select(ThreadSummaryArtifact)
                            .where(
                                ThreadSummaryArtifact.workspace_id == workspace_id,
                                ThreadSummaryArtifact.thread_id == thread_id,
                                ThreadSummaryArtifact.job_id == job.id,
                                ThreadSummaryArtifact.id == candidate.artifact_id,
                                ThreadSummaryArtifact.publication_status
                                == ThreadSummaryArtifactStatus.PUBLISHED.value,
                            )
                            .with_for_update(read=True)
                        )
                        response = await session.scalar(
                            select(ThreadSummaryResponse).where(
                                ThreadSummaryResponse.workspace_id == workspace_id,
                                ThreadSummaryResponse.thread_id == thread_id,
                                ThreadSummaryResponse.job_id == job.id,
                                ThreadSummaryResponse.id == candidate.response_id,
                            )
                        )
                        if artifact is None or response is None:
                            continue
                        source_rows = await self._load_sources(session, candidate.key)
                        if not _artifact_is_integrity_valid(
                            artifact, job, response, source_rows
                        ) or not _source_index_matches_prefix(artifact.source_index, candidates):
                            continue
                        await self._trusted_dispatch_context(session, candidate.key, job)
            except SummaryServiceError as error:
                if error.code in permission_or_lease_rejections:
                    raise
                continue
            return candidate
        return None

    async def get_published_revision(self, key: SummaryJobKey) -> PublishedSummaryRevision | None:
        """Reload the exact published job revision and recheck all stored hashes."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ThreadSummaryArtifact,
                        ThreadSummaryJob,
                        ThreadSummaryResponse,
                    )
                    .join(
                        ThreadSummaryJob,
                        (ThreadSummaryJob.workspace_id == ThreadSummaryArtifact.workspace_id)
                        & (ThreadSummaryJob.thread_id == ThreadSummaryArtifact.thread_id)
                        & (ThreadSummaryJob.id == ThreadSummaryArtifact.job_id),
                    )
                    .join(
                        ThreadSummaryResponse,
                        (ThreadSummaryResponse.workspace_id == ThreadSummaryArtifact.workspace_id)
                        & (ThreadSummaryResponse.thread_id == ThreadSummaryArtifact.thread_id)
                        & (ThreadSummaryResponse.job_id == ThreadSummaryArtifact.job_id)
                        & (ThreadSummaryResponse.id == ThreadSummaryArtifact.response_id),
                    )
                    .where(
                        ThreadSummaryArtifact.workspace_id == key.workspace_id,
                        ThreadSummaryArtifact.thread_id == key.thread_id,
                        ThreadSummaryJob.id == key.job_id,
                        ThreadSummaryArtifact.publication_status
                        == ThreadSummaryArtifactStatus.PUBLISHED.value,
                        ThreadSummaryJob.status == SummaryJobStatus.PUBLISHED.value,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            artifact, job, response = row
            strategy_error = self._request_strategy_error(job)
            if strategy_error is not None:
                raise strategy_error
            sources = await self._load_sources(session, key)
            if not _artifact_is_integrity_valid(artifact, job, response, sources):
                raise SummaryServiceError(
                    "SUMMARY_ARTIFACT_INTEGRITY_INVALID",
                    "The published summary revision no longer matches its identity.",
                )
            validation = dict(artifact.validation_result)
            expected_ids = [item["source_id"] for item in artifact.source_index]
            if validation.get("covered_source_ids") != expected_ids or artifact.summary_content.get(
                "omitted_source_ids"
            ):
                raise SummaryServiceError(
                    "SUMMARY_ARTIFACT_COVERAGE_INVALID",
                    "The published summary does not cover its complete source prefix.",
                )
            return PublishedSummaryRevision(
                key=key,
                artifact_id=artifact.id,
                response_id=response.id,
                revision_number=artifact.revision_number,
                revision_hash=artifact.revision_hash,
                content_hash=artifact.content_hash,
                summary_content=dict(artifact.summary_content),
                source_index=tuple(dict(item) for item in artifact.source_index),
                validation_result=validation,
                policy_version=job.policy_version,
                prompt_version=job.prompt_version,
                source_scope=dict(job.source_scope),
            )

    async def resolve_applicable_pricing(
        self,
        *,
        workspace_id: UUID,
        provider: str,
        model: str,
        effective_before: datetime,
    ) -> UUID:
        """Resolve one reviewed immutable price or refuse the summary dispatch."""
        async with self._session_factory() as session:
            snapshot = await session.scalar(
                select(PricingSnapshot)
                .where(
                    PricingSnapshot.workspace_id == workspace_id,
                    PricingSnapshot.provider == provider,
                    PricingSnapshot.model == model,
                    PricingSnapshot.effective_at <= effective_before,
                )
                .order_by(
                    PricingSnapshot.effective_at.desc(),
                    PricingSnapshot.created_at.desc(),
                    PricingSnapshot.id.desc(),
                )
                .limit(1)
            )
        if snapshot is None or snapshot.content_hash != _pricing_snapshot_content_hash(snapshot):
            raise SummaryServiceError(
                "SUMMARY_PRICING_UNAVAILABLE",
                "No reviewed immutable price matches the frozen summary model.",
            )
        return snapshot.id

    async def get_settlement(self, key: SummaryJobKey) -> SummarySettlement:
        """Read the durable result without mutating any failure or unknown state."""

        async with self._session_factory() as session:
            job = await session.scalar(
                select(ThreadSummaryJob).where(
                    ThreadSummaryJob.workspace_id == key.workspace_id,
                    ThreadSummaryJob.thread_id == key.thread_id,
                    ThreadSummaryJob.id == key.job_id,
                )
            )
            if job is None:
                raise SummaryServiceError("SUMMARY_JOB_NOT_FOUND", "The summary job was not found.")
            return _settlement(job)

    async def _verify(
        self,
        session: AsyncSession,
        binding: SummaryJobBinding,
    ) -> SummaryTrustDecision:
        try:
            decision = await self._verifier.verify(session, binding)
        except Exception:
            return SummaryTrustDecision.unavailable("TRUSTED_VERIFIER_FAILED")
        if not isinstance(decision, SummaryTrustDecision):
            return SummaryTrustDecision.unavailable("TRUSTED_VERIFIER_CONTRACT_INVALID")
        return decision

    async def _verify_exact_checkpoint(
        self,
        command: NewSummaryJob,
    ) -> VerifiedCheckpointBinding:
        adapter = self._checkpoint_adapter
        if adapter is None:
            raise SummaryServiceError(
                "SUMMARY_CHECKPOINT_VERIFIER_UNAVAILABLE",
                "Exact LangGraph checkpoint verification is not configured.",
            )
        if not isinstance(command.checkpoint_id, str) or command.checkpoint_ns is None:
            raise SummaryServiceError(
                "SUMMARY_CHECKPOINT_SELECTOR_INVALID",
                "A checkpoint ID and namespace are required.",
            )
        try:
            return await verify_exact_checkpoint_binding(
                self._session_factory,
                checkpoint_adapter=adapter,
                workspace_id=command.workspace_id,
                thread_id=command.thread_id,
                run_id=command.trigger_run_id,
                checkpoint_id=command.checkpoint_id,
                checkpoint_ns=command.checkpoint_ns,
            )
        except AgentHubError:
            raise SummaryServiceError(
                "SUMMARY_CHECKPOINT_BINDING_INVALID",
                "The selected LangGraph checkpoint is not trustworthy for this summary.",
            ) from None

    async def _verify_fixed_job_checkpoint(
        self,
        job: ThreadSummaryJob,
    ) -> VerifiedCheckpointBinding:
        _require_model_round_boundary(job)
        adapter = self._checkpoint_adapter
        if adapter is None:
            raise SummaryServiceError(
                "SUMMARY_CHECKPOINT_VERIFIER_UNAVAILABLE",
                "Exact LangGraph checkpoint verification is not configured.",
            )
        anchor = _checkpoint_anchor(job)
        try:
            return await verify_fixed_checkpoint_binding(
                self._session_factory,
                checkpoint_adapter=adapter,
                expected=anchor,
            )
        except AgentHubError:
            raise SummaryServiceError(
                "SUMMARY_CHECKPOINT_BINDING_INVALID",
                "The summary job's fixed LangGraph checkpoint is no longer valid.",
            ) from None

    async def _trusted_dispatch_context(
        self, session: AsyncSession, key: SummaryJobKey, job: ThreadSummaryJob
    ) -> SummaryInputContext:
        strategy_error = self._request_strategy_error(job)
        if strategy_error is not None:
            raise strategy_error
        await self._verify_fixed_job_checkpoint(job)
        if job.tokenizer_id != "approx_language_v1" or job.tokenizer_version != "1":
            raise SummaryServiceError(
                "SUMMARY_COUNTER_UNSUPPORTED", "The frozen summary token counter is unavailable."
            )
        sources = await self._load_sources(session, key)
        _verify_job_source_scope(key, job, sources=sources)
        decision = await self._verify(session, _binding(job, SummaryVerifierPhase.DISPATCH))
        if decision.status is SummaryTrustStatus.UNAVAILABLE:
            raise SummaryServiceError(
                "SUMMARY_DISPATCH_TRUST_UNAVAILABLE",
                "Trusted actor, source, and eligibility verification is unavailable.",
            )
        if decision.status is SummaryTrustStatus.DENIED:
            raise SummaryServiceError(
                _failure_code(decision, "SUMMARY_DISPATCH_BINDING_STALE"),
                "The actor, frozen sources, or source eligibility is no longer valid.",
            )
        context = decision.validation_context
        if context is None:
            raise SummaryServiceError(
                "SUMMARY_DISPATCH_TRUST_CONTEXT_MISSING",
                "Trusted dispatch verification did not return source evidence.",
            )
        if not _context_matches_job(context, job, sources):
            raise SummaryServiceError(
                "SUMMARY_DISPATCH_TRUST_CONTEXT_MISMATCH",
                "Trusted dispatch verification does not match the frozen source manifest.",
            )
        return context

    async def _persist_validated_artifact(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
        job: ThreadSummaryJob,
        response: ThreadSummaryResponse,
        sources: Sequence[ThreadSummaryJobSource],
        validation: SummaryValidationResult,
        *,
        lease_token: str,
        now: datetime,
        clock_override: datetime | None,
    ) -> SummaryServiceError | None:
        await self._lock_summary_thread(session, key)
        if not _owns_live_lease(job, lease_token, self._now(clock_override)):
            raise SummaryServiceError(
                "SUMMARY_LEASE_LOST", "The validation lease expired while allocating a revision."
            )
        parent = await self._validate_parent(
            session,
            workspace_id=key.workspace_id,
            thread_id=key.thread_id,
            parent_artifact_id=job.parent_artifact_id,
            parent_revision_hash=job.parent_revision_hash,
            lock=True,
        )
        if parent is None and job.parent_artifact_id is not None:
            _mark_stale(job, now, "SUMMARY_PARENT_REVISION_INVALID")
            return SummaryServiceError(
                "SUMMARY_PARENT_REVISION_INVALID",
                "The parent summary revision is no longer valid.",
            )
        latest = await session.scalar(
            select(ThreadSummaryArtifact)
            .where(
                ThreadSummaryArtifact.workspace_id == key.workspace_id,
                ThreadSummaryArtifact.thread_id == key.thread_id,
                ThreadSummaryArtifact.publication_status
                == ThreadSummaryArtifactStatus.PUBLISHED.value,
            )
            .order_by(ThreadSummaryArtifact.revision_number.desc())
            .limit(1)
            .with_for_update(read=True)
        )
        if job.parent_artifact_id is None:
            if latest is not None:
                _mark_stale(job, now, "SUMMARY_PARENT_REVISION_MISSING")
                return SummaryServiceError(
                    "SUMMARY_PARENT_REVISION_MISSING",
                    "A newer published summary exists for this Thread.",
                )
        else:
            if latest is None or latest.id != job.parent_artifact_id:
                _mark_stale(job, now, "SUMMARY_PARENT_REVISION_SUPERSEDED")
                return SummaryServiceError(
                    "SUMMARY_PARENT_REVISION_SUPERSEDED",
                    "The job does not extend the current published summary.",
                )

        next_revision = await session.scalar(
            select(func.max(ThreadSummaryArtifact.revision_number)).where(
                ThreadSummaryArtifact.workspace_id == key.workspace_id,
                ThreadSummaryArtifact.thread_id == key.thread_id,
            )
        )
        revision_number = (next_revision or 0) + 1

        assert validation.candidate is not None
        summary_content = validation.candidate.as_dict()
        content_hash = canonical_json_hash(summary_content)
        source_index = [_source_index_entry(source) for source in sources]
        source_index_hash = canonical_json_hash(
            {
                "version": SUMMARY_SOURCE_INDEX_VERSION,
                "sources": source_index,
            }
        )
        summary_identity_hash = canonical_json_hash(
            {
                "version": SUMMARY_ARTIFACT_VERSION,
                "job_identity_hash": job.identity_hash,
                "content_hash": content_hash,
            }
        )
        validation_result = validation.as_dict()
        revision_hash = canonical_json_hash(
            {
                "version": SUMMARY_ARTIFACT_VERSION,
                "workspace_id": str(key.workspace_id),
                "thread_id": str(key.thread_id),
                "job_identity_hash": job.identity_hash,
                "response_sha256": response.response_sha256,
                "parent_artifact_id": (
                    str(job.parent_artifact_id) if job.parent_artifact_id is not None else None
                ),
                "parent_revision_hash": job.parent_revision_hash,
                "revision_number": revision_number,
                "content_hash": content_hash,
                "source_index_hash": source_index_hash,
                "validator_version": validation.validator_version,
            }
        )
        artifact = ThreadSummaryArtifact(
            id=uuid4(),
            workspace_id=key.workspace_id,
            thread_id=key.thread_id,
            job_id=key.job_id,
            response_id=response.id,
            parent_artifact_id=job.parent_artifact_id,
            parent_revision_hash=job.parent_revision_hash,
            revision_number=revision_number,
            revision_hash=revision_hash,
            content_hash=content_hash,
            summary_identity_hash=summary_identity_hash,
            summary_content=summary_content,
            source_index=source_index,
            source_index_hash=source_index_hash,
            validator_version=validation.validator_version,
            validation_result=validation_result,
        )
        try:
            async with session.begin_nested():
                session.add(artifact)
                await session.flush()
        except DBAPIError as error:
            _mark_stale(job, now, "SUMMARY_ARTIFACT_RECONCILIATION_REQUIRED")
            return SummaryServiceError(
                "SUMMARY_ARTIFACT_RECONCILIATION_REQUIRED",
                "The immutable artifact revision could not be allocated.",
            ).with_traceback(error.__traceback__)

        state = validate_response(
            _state(job),
            lease_token=lease_token,
            valid=True,
        )
        _apply_state(job, state)
        job.artifact_id = artifact.id
        job.validation_result = validation_result
        job.validated_at = now
        job.failure_code = None
        await session.flush()
        if not _owns_live_lease(job, lease_token, self._now(clock_override)):
            raise SummaryServiceError(
                "SUMMARY_LEASE_LOST", "The validation lease expired before the revision committed."
            )
        return None

    async def _parent_is_current(
        self,
        session: AsyncSession,
        job: ThreadSummaryJob,
    ) -> tuple[bool, str | None]:
        if job.parent_artifact_id is None:
            latest_id = await session.scalar(
                select(ThreadSummaryArtifact.id)
                .where(
                    ThreadSummaryArtifact.workspace_id == job.workspace_id,
                    ThreadSummaryArtifact.thread_id == job.thread_id,
                    ThreadSummaryArtifact.publication_status
                    == ThreadSummaryArtifactStatus.PUBLISHED.value,
                )
                .order_by(ThreadSummaryArtifact.revision_number.desc())
                .limit(1)
                .with_for_update(read=True)
            )
            return (
                latest_id is None,
                None if latest_id is None else "SUMMARY_PARENT_REVISION_MISSING",
            )
        parent = await self._validate_parent(
            session,
            workspace_id=job.workspace_id,
            thread_id=job.thread_id,
            parent_artifact_id=job.parent_artifact_id,
            parent_revision_hash=job.parent_revision_hash,
            lock=True,
            require_published=True,
        )
        if parent is None:
            return False, "SUMMARY_PARENT_REVISION_INVALID"
        latest_id = await session.scalar(
            select(ThreadSummaryArtifact.id)
            .where(
                ThreadSummaryArtifact.workspace_id == job.workspace_id,
                ThreadSummaryArtifact.thread_id == job.thread_id,
                ThreadSummaryArtifact.publication_status
                == ThreadSummaryArtifactStatus.PUBLISHED.value,
            )
            .order_by(ThreadSummaryArtifact.revision_number.desc())
            .limit(1)
            .with_for_update(read=True)
        )
        if latest_id != parent.id:
            return False, "SUMMARY_PARENT_REVISION_SUPERSEDED"
        return True, None

    async def _validate_parent(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        parent_artifact_id: UUID | None,
        parent_revision_hash: str | None,
        lock: bool = False,
        require_published: bool = True,
    ) -> ThreadSummaryArtifact | None:
        if (parent_artifact_id is None) != (parent_revision_hash is None):
            raise SummaryServiceError(
                "SUMMARY_PARENT_INVALID",
                "Parent artifact and revision hash must be supplied together.",
            )
        if parent_artifact_id is None:
            return None
        statement = select(ThreadSummaryArtifact).where(
            ThreadSummaryArtifact.workspace_id == workspace_id,
            ThreadSummaryArtifact.thread_id == thread_id,
            ThreadSummaryArtifact.id == parent_artifact_id,
            ThreadSummaryArtifact.revision_hash == parent_revision_hash,
        )
        if lock:
            statement = statement.with_for_update(read=True)
        artifact = await session.scalar(statement)
        if artifact is None:
            return None
        if (
            require_published
            and artifact.publication_status != ThreadSummaryArtifactStatus.PUBLISHED.value
        ):
            return None
        return artifact

    async def _lock_job(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
    ) -> ThreadSummaryJob | None:
        return await session.scalar(
            select(ThreadSummaryJob)
            .where(
                ThreadSummaryJob.workspace_id == key.workspace_id,
                ThreadSummaryJob.thread_id == key.thread_id,
                ThreadSummaryJob.id == key.job_id,
            )
            .with_for_update()
        )

    async def _lock_summary_thread(self, session: AsyncSession, key: SummaryJobKey) -> None:
        """Serialize revision allocation and publication, including an empty history."""
        thread_id = await session.scalar(
            select(AgentThread.id)
            .where(AgentThread.workspace_id == key.workspace_id, AgentThread.id == key.thread_id)
            .with_for_update()
        )
        if thread_id is None:
            raise SummaryServiceError(
                "SUMMARY_THREAD_NOT_FOUND", "The summary Thread is unavailable."
            )

    async def _lock_dispatch(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
        dispatch_id: UUID | None,
    ) -> ThreadSummaryDispatch | None:
        if dispatch_id is None:
            return None
        return await session.scalar(
            select(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
                ThreadSummaryDispatch.id == dispatch_id,
            )
            .with_for_update()
        )

    async def _lock_response(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
        response_id: UUID | None,
    ) -> ThreadSummaryResponse | None:
        if response_id is None:
            return None
        return await session.scalar(
            select(ThreadSummaryResponse).where(
                ThreadSummaryResponse.workspace_id == key.workspace_id,
                ThreadSummaryResponse.thread_id == key.thread_id,
                ThreadSummaryResponse.job_id == key.job_id,
                ThreadSummaryResponse.id == response_id,
            )
        )

    async def _lock_artifact(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
        artifact_id: UUID | None,
    ) -> ThreadSummaryArtifact | None:
        if artifact_id is None:
            return None
        return await session.scalar(
            select(ThreadSummaryArtifact)
            .where(
                ThreadSummaryArtifact.workspace_id == key.workspace_id,
                ThreadSummaryArtifact.thread_id == key.thread_id,
                ThreadSummaryArtifact.job_id == key.job_id,
                ThreadSummaryArtifact.id == artifact_id,
            )
            .with_for_update()
        )

    async def _load_sources(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
    ) -> tuple[ThreadSummaryJobSource, ...]:
        return tuple(
            await session.scalars(
                select(ThreadSummaryJobSource)
                .where(
                    ThreadSummaryJobSource.workspace_id == key.workspace_id,
                    ThreadSummaryJobSource.thread_id == key.thread_id,
                    ThreadSummaryJobSource.job_id == key.job_id,
                )
                .order_by(ThreadSummaryJobSource.ordinal)
            )
        )

    async def _mark_dispatch_unknown(
        self,
        session: AsyncSession,
        key: SummaryJobKey,
        dispatch: ThreadSummaryDispatch,
        now: datetime,
    ) -> None:
        result = await session.execute(
            update(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
                ThreadSummaryDispatch.id == dispatch.id,
                ThreadSummaryDispatch.outcome.in_(
                    (
                        ThreadSummaryDispatchOutcome.INTENT_RECORDED.value,
                        ThreadSummaryDispatchOutcome.DISPATCHED.value,
                    )
                ),
            )
            .values(
                outcome=ThreadSummaryDispatchOutcome.UNKNOWN_OUTCOME.value,
                settled_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1 and dispatch.outcome != (
            ThreadSummaryDispatchOutcome.UNKNOWN_OUTCOME.value
        ):
            raise SummaryServiceError(
                "SUMMARY_DISPATCH_INVARIANT",
                "The dispatch ledger could not be marked unknown.",
            )
        mark_budget_unknown = getattr(self._budget_reserver, "mark_unknown_outcome", None)
        if callable(mark_budget_unknown):
            job = await session.scalar(
                select(ThreadSummaryJob).where(
                    ThreadSummaryJob.workspace_id == key.workspace_id,
                    ThreadSummaryJob.thread_id == key.thread_id,
                    ThreadSummaryJob.id == key.job_id,
                )
            )
            if job is None:
                raise SummaryServiceError(
                    "SUMMARY_DISPATCH_INVARIANT", "The budget's summary job is missing."
                )
            await mark_budget_unknown(
                session,
                binding=_binding(job, SummaryVerifierPhase.DISPATCH),
                reservation_id=dispatch.reservation_id,
            )

    async def _mark_artifact_stale(
        self,
        session: AsyncSession,
        artifact: ThreadSummaryArtifact,
    ) -> None:
        result = await session.execute(
            update(ThreadSummaryArtifact)
            .where(
                ThreadSummaryArtifact.workspace_id == artifact.workspace_id,
                ThreadSummaryArtifact.thread_id == artifact.thread_id,
                ThreadSummaryArtifact.id == artifact.id,
                ThreadSummaryArtifact.publication_status
                == ThreadSummaryArtifactStatus.PENDING.value,
            )
            .values(publication_status=ThreadSummaryArtifactStatus.STALE.value)
        )
        if result.rowcount != 1:
            raise SummaryServiceError(
                "SUMMARY_ARTIFACT_INVARIANT",
                "The pending artifact could not be marked stale.",
            )

    def _now(self, value: datetime | None) -> datetime:
        current = value or self._clock()
        if not isinstance(current, datetime):
            raise TypeError("clock must return a datetime")
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current.astimezone(UTC)

    def _lease_duration(self, value: int | None) -> int:
        duration = self._lease_seconds if value is None else value
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ValueError("lease duration must be a positive integer")
        return duration

    def _recovery_limit(self, value: int | None) -> int:
        limit = self._max_recovery_batch if value is None else value
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("recovery limit must be a positive integer")
        return min(limit, self._max_recovery_batch)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _state(job: ThreadSummaryJob) -> SummaryState:
    return SummaryState(
        status=SummaryJobStatus(job.status),
        lease_token=job.lease_token,
        budget_reserved=job.budget_reserved,
        response_ref=str(job.response_id) if job.response_id is not None else None,
        revision_ref=str(job.artifact_id) if job.artifact_id is not None else None,
    )


def _apply_state(job: ThreadSummaryJob, state: SummaryState) -> None:
    job.status = state.status.value
    job.lease_token = state.lease_token
    job.budget_reserved = state.budget_reserved


def _settlement(
    job: ThreadSummaryJob,
    *,
    validation: SummaryValidationResult | None = None,
) -> SummarySettlement:
    validation_result = validation.as_dict() if validation is not None else job.validation_result
    return SummarySettlement(
        key=SummaryJobKey(job.workspace_id, job.thread_id, job.id),
        status=job.status,
        response_id=job.response_id,
        artifact_id=job.artifact_id,
        validation_result=validation_result,
    )


def _boundary_conflict(existing: ThreadSummaryJob) -> SummaryServiceError:
    return SummaryServiceError(
        "SUMMARY_DISPATCH_BOUNDARY_CONFLICT",
        (
            f"Summary job {existing.id} already owns trigger Run "
            f"{existing.trigger_run_id} model round {existing.model_round} "
            f"with identity {existing.identity_hash}."
        ),
    )


def _require_model_round_boundary(job: ThreadSummaryJob) -> None:
    if not _positive_int(job.model_round) or job.model_round > MAX_SUMMARY_MODEL_ROUND:
        raise SummaryServiceError(
            "SUMMARY_DISPATCH_BOUNDARY_REQUIRED",
            "A legacy summary job without a model-round boundary cannot dispatch.",
        )
    checkpoint = _checkpoint_anchor(job)
    if job.model_round != checkpoint.checkpoint_model_round_count + 1:
        raise SummaryServiceError(
            "SUMMARY_DISPATCH_BOUNDARY_INVALID",
            "The summary model round does not follow its fixed checkpoint.",
        )


def _checkpoint_anchor(job: ThreadSummaryJob) -> PersistedCheckpointBinding:
    checkpoint_id = getattr(job, "checkpoint_id", None)
    checkpoint_ns = getattr(job, "checkpoint_ns", None)
    checkpoint_model_round_count = getattr(job, "checkpoint_model_round_count", None)
    checkpoint_binding_sha256 = getattr(job, "checkpoint_binding_sha256", None)
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(checkpoint_ns, str)
        or not _non_negative_int(checkpoint_model_round_count)
        or checkpoint_model_round_count >= MAX_SUMMARY_MODEL_ROUND
        or not _sha256(checkpoint_binding_sha256)
    ):
        raise SummaryServiceError(
            "SUMMARY_DISPATCH_BOUNDARY_REQUIRED",
            "The summary job has no complete fixed LangGraph checkpoint identity.",
        )
    return PersistedCheckpointBinding(
        workspace_id=job.workspace_id,
        thread_id=job.thread_id,
        run_id=job.trigger_run_id,
        checkpoint_id=checkpoint_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_model_round_count=checkpoint_model_round_count,
        checkpoint_binding_sha256=checkpoint_binding_sha256,
    )


def _trusted_summary_model_round(
    command: NewSummaryJob,
    checkpoint: VerifiedCheckpointBinding,
) -> int:
    model_round = checkpoint.checkpoint_model_round_count + 1
    if model_round > MAX_SUMMARY_MODEL_ROUND:
        raise SummaryServiceError(
            "SUMMARY_CHECKPOINT_ROUND_INVALID",
            "The checkpoint has no room for another model round.",
        )
    if command.model_round is not None and command.model_round != model_round:
        raise SummaryServiceError(
            "SUMMARY_CHECKPOINT_ROUND_MISMATCH",
            "The asserted model round does not follow the exact checkpoint.",
        )
    return model_round


def _binding(job: ThreadSummaryJob, phase: SummaryVerifierPhase) -> SummaryJobBinding:
    return SummaryJobBinding(
        workspace_id=job.workspace_id,
        thread_id=job.thread_id,
        job_id=job.id,
        trigger_run_id=job.trigger_run_id,
        model_round=job.model_round,
        created_by=job.created_by,
        identity_hash=job.identity_hash,
        source_watermark=job.source_watermark,
        source_scope=dict(job.source_scope),
        source_scope_hash=job.source_scope_hash,
        source_content_hash=job.source_content_hash,
        parent_artifact_id=job.parent_artifact_id,
        parent_revision_hash=job.parent_revision_hash,
        policy_version=job.policy_version,
        prompt_version=job.prompt_version,
        model_spec_hash=job.model_spec_hash,
        permission_snapshot_hash=job.permission_snapshot_hash,
        response_id=job.response_id,
        artifact_id=job.artifact_id,
        phase=phase,
        checkpoint_id=getattr(job, "checkpoint_id", None),
        checkpoint_ns=getattr(job, "checkpoint_ns", None),
        checkpoint_model_round_count=getattr(job, "checkpoint_model_round_count", None),
        checkpoint_binding_sha256=getattr(job, "checkpoint_binding_sha256", None),
    )


def _summary_identity_hash(
    command: NewSummaryJob,
    *,
    checkpoint: VerifiedCheckpointBinding,
    model_round: int,
) -> str:
    _validate_new_job(command)
    return canonical_json_hash(
        {
            "version": SUMMARY_JOB_IDENTITY_VERSION,
            "workspace_id": str(command.workspace_id),
            "thread_id": str(command.thread_id),
            "trigger_run_id": str(command.trigger_run_id),
            "model_round": model_round,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_ns": checkpoint.checkpoint_ns,
            "checkpoint_model_round_count": (checkpoint.checkpoint_model_round_count),
            "checkpoint_binding_sha256": checkpoint.checkpoint_binding_sha256,
            "source_watermark": command.source_watermark,
            "source_scope": dict(command.source_scope),
            "source_content_hash": _source_content_hash(command),
            "parent_artifact_id": (
                str(command.parent_artifact_id) if command.parent_artifact_id is not None else None
            ),
            "parent_revision_hash": command.parent_revision_hash,
            "policy_spec": dict(command.policy_spec),
            "policy_version": command.policy_version,
            "prompt_version": command.prompt_version,
            "budget_version": command.budget_version,
            "model_spec": dict(command.model_spec),
            "tokenizer_id": command.tokenizer_id,
            "tokenizer_version": command.tokenizer_version,
            "target_budget_tokens": command.target_budget_tokens,
            "permission_snapshot_hash": command.permission_snapshot_hash,
        }
    )


def _source_content_hash(command: NewSummaryJob) -> str:
    return canonical_json_hash(
        {
            "version": "thread_summary_source_content_v1",
            "sources": [
                {
                    "source_id": source.source_id,
                    "turn_id": str(source.turn_id),
                    "run_id": str(source.run_id),
                    "sequence": source.sequence,
                    "original_ref": source.original_ref,
                    "source_identity_hash": source.source_identity_hash,
                    "revision_sha256": source.revision_sha256,
                    "user_input_sha256": source.user_input_sha256,
                    "final_output_sha256": source.final_output_sha256,
                    "artifact_identities": [dict(item) for item in source.artifact_identities],
                    "summary_eligibility": source.summary_eligibility,
                    "eligibility_reason": source.eligibility_reason,
                }
                for source in sorted(command.sources, key=lambda item: item.sequence)
            ],
        }
    )


def _validate_new_job(command: NewSummaryJob) -> None:
    for name in ("workspace_id", "thread_id", "trigger_run_id", "created_by"):
        if not isinstance(getattr(command, name), UUID):
            raise TypeError(f"{name} must be a UUID")
    if (command.parent_artifact_id is None) != (command.parent_revision_hash is None):
        raise SummaryServiceError(
            "SUMMARY_PARENT_INVALID",
            "Parent artifact and revision hash must be supplied together.",
        )
    if not _positive_int(command.source_watermark):
        raise ValueError("source_watermark must be a positive integer")
    if not _positive_int(command.target_budget_tokens):
        raise ValueError("target_budget_tokens must be a positive integer")
    if not 1 <= len(command.sources) <= MAX_SOURCES_PER_JOB:
        raise SummaryServiceError(
            "SUMMARY_SOURCE_COUNT_INVALID",
            f"A summary job requires 1-{MAX_SOURCES_PER_JOB} sources.",
        )
    sequences = [source.sequence for source in command.sources]
    source_ids = [source.source_id for source in command.sources]
    if len(set(sequences)) != len(sequences) or len(set(source_ids)) != len(source_ids):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_DUPLICATE",
            "Source IDs and sequences must be unique.",
        )
    if sequences != sorted(sequences):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_ORDER_INVALID",
            "Sources must be supplied in ascending sequence order.",
        )
    if any(
        not _positive_int(sequence) or sequence >= command.source_watermark
        for sequence in sequences
    ):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_OUT_OF_SCOPE",
            "Every source must be before the frozen watermark.",
        )
    for source in command.sources:
        for name in ("turn_id", "run_id"):
            if not isinstance(getattr(source, name), UUID):
                raise TypeError(f"source {name} must be a UUID")
        if source.summary_eligibility not in {"ELIGIBLE", "INELIGIBLE", "UNKNOWN"}:
            raise SummaryServiceError(
                "SUMMARY_SOURCE_ELIGIBILITY_INVALID",
                "Source summary eligibility is invalid.",
            )
        for value in (
            source.source_identity_hash,
            source.revision_sha256,
            source.user_input_sha256,
            source.final_output_sha256,
        ):
            if not _sha256(value):
                raise SummaryServiceError(
                    "SUMMARY_SOURCE_HASH_INVALID",
                    "A source hash is not lowercase SHA-256.",
                )
    for value in (command.permission_snapshot_hash, command.parent_revision_hash):
        if value is not None and not _sha256(value):
            raise SummaryServiceError(
                "SUMMARY_JOB_HASH_INVALID",
                "A job binding hash is not lowercase SHA-256.",
            )
    for name in (
        "policy_version",
        "prompt_version",
        "budget_version",
        "tokenizer_id",
        "tokenizer_version",
    ):
        value = getattr(command, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    try:
        canonical_json_hash(dict(command.source_scope))
        canonical_json_hash(dict(command.policy_spec))
        canonical_json_hash(dict(command.model_spec))
    except (TypeError, ValueError) as error:
        raise SummaryServiceError(
            "SUMMARY_JOB_JSON_INVALID",
            "Job metadata must be canonical JSON.",
        ) from error
    if not isinstance(command.checkpoint_id, str) or not command.checkpoint_id:
        raise ValueError("checkpoint_id must be a non-empty string")
    if not isinstance(command.checkpoint_ns, str):
        raise ValueError("checkpoint_ns must be a string")
    try:
        checkpoint_id_bytes = len(command.checkpoint_id.encode("utf-8"))
        checkpoint_ns_bytes = len(command.checkpoint_ns.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("checkpoint selector must be valid UTF-8 text") from None
    if checkpoint_id_bytes > 1024 or checkpoint_ns_bytes > 1024:
        raise ValueError("checkpoint selectors must not exceed 1024 UTF-8 bytes")
    if command.model_round is not None and (
        not _positive_int(command.model_round) or command.model_round > MAX_SUMMARY_MODEL_ROUND
    ):
        raise ValueError(
            f"model_round must be a positive integer no greater than {MAX_SUMMARY_MODEL_ROUND}"
        )


def _response_bound_failure(
    response: ThreadSummaryResponse, dispatch: ThreadSummaryDispatch
) -> str | None:
    """Keep observed cost, but never promote usage outside the reservation."""
    if response.usage_metadata.get("cost_status") == "INVALID_REPORTED":
        return "SUMMARY_PROVIDER_COST_UNVERIFIED"
    if response.input_tokens is None or response.output_tokens is None:
        return "SUMMARY_PROVIDER_USAGE_UNVERIFIED"
    if (
        response.input_tokens > dispatch.reserved_input_tokens
        or response.output_tokens > dispatch.reserved_output_tokens
    ):
        return "SUMMARY_PROVIDER_USAGE_EXCEEDS_RESERVATION"
    if response.cost_amount is not None:
        if response.cost_currency != dispatch.reserved_currency:
            return "SUMMARY_PROVIDER_COST_CURRENCY_MISMATCH"
        if (
            dispatch.reserved_cost_amount is None
            or response.cost_amount > dispatch.reserved_cost_amount
        ):
            return "SUMMARY_PROVIDER_COST_EXCEEDS_RESERVATION"
    return None


def _validate_response_data(response: SummaryResponseData) -> None:
    if not isinstance(response, SummaryResponseData):
        raise TypeError("response must be SummaryResponseData")
    if not isinstance(response.provider, str) or not response.provider.strip():
        raise ValueError("provider must be a non-empty string")
    if not isinstance(response.model, str) or not response.model.strip():
        raise ValueError("model must be a non-empty string")
    usage_values = (
        response.input_tokens,
        response.output_tokens,
        response.total_tokens,
    )
    if any(value is not None for value in usage_values):
        if any(not _non_negative_int(value) for value in usage_values):
            raise SummaryServiceError(
                "SUMMARY_RESPONSE_USAGE_INVALID",
                "Input, output and total tokens must all be present.",
            )
        assert response.input_tokens is not None
        assert response.output_tokens is not None
        assert response.total_tokens is not None
        if response.total_tokens != response.input_tokens + response.output_tokens:
            raise SummaryServiceError(
                "SUMMARY_RESPONSE_USAGE_INVALID",
                "Total tokens must equal input plus output tokens.",
            )
    if response.cached_tokens is not None and not _non_negative_int(response.cached_tokens):
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_USAGE_INVALID",
            "Cached tokens must be non-negative.",
        )
    cost_values = (
        response.cost_amount,
        response.cost_currency,
        response.cost_is_estimate,
    )
    if any(value is not None for value in cost_values) and any(
        value is None for value in cost_values
    ):
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_COST_INVALID",
            "Response cost amount, currency and estimate flag must be supplied together.",
        )
    if response.cost_amount is not None and response.cost_amount < 0:
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_COST_INVALID",
            "Response cost must be non-negative.",
        )
    if response.cost_currency is not None and not _currency(response.cost_currency):
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_COST_INVALID",
            "Response currency must be a three-letter code.",
        )


def _encode_raw_response(
    value: Mapping[str, Any] | str | bytes,
) -> tuple[dict[str, str], str]:
    """Store provider bytes without parsing, including malformed JSON and NULs."""
    if isinstance(value, Mapping):
        raw = canonical_json(dict(value)).encode("utf-8")
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="strict")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise TypeError("raw_response must be a mapping, string, or bytes")
    envelope = {"format": "summary_raw_base64_v1", "data": base64.b64encode(raw).decode("ascii")}
    if len(json.dumps(envelope).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("encoded response exceeds the byte limit")
    return envelope, hashlib.sha256(raw).hexdigest()


def _decode_raw_response(response: ThreadSummaryResponse) -> bytes:
    envelope = response.raw_response
    try:
        if (
            isinstance(envelope, dict)
            and set(envelope) == {"format", "data"}
            and envelope["format"] == "summary_raw_base64_v1"
            and isinstance(envelope["data"], str)
        ):
            raw = base64.b64decode(envelope["data"], validate=True)
        elif isinstance(envelope, dict):
            # Compatibility for isolated pre-release rows stored as parsed JSON.
            raw = canonical_json(envelope).encode("utf-8")
        else:
            raise ValueError("stored response envelope is invalid")
        if hashlib.sha256(raw).hexdigest() != response.response_sha256:
            raise ValueError("stored response identity differs")
        return raw
    except (ValueError, TypeError, UnicodeError, binascii.Error) as error:
        raise SummaryServiceError(
            "SUMMARY_RESPONSE_INVARIANT", "The persisted provider response is invalid."
        ) from error


def _context_matches_job(
    context: SummaryInputContext,
    job: ThreadSummaryJob,
    sources: Sequence[ThreadSummaryJobSource],
) -> bool:
    if (
        context.workspace_id != str(job.workspace_id)
        or context.thread_id != str(job.thread_id)
        or context.watermark != job.source_watermark
        or not _permission_hash_matches(context.permission_snapshot, job.permission_snapshot_hash)
        or len(context.sources) != len(sources)
        or context.prefix_first_sequence != sources[0].sequence
        or context.prefix_last_sequence != sources[-1].sequence
    ):
        return False
    for prompt_source, stored in zip(context.sources, sources, strict=True):
        if (
            prompt_source.source_id != stored.source_id
            or prompt_source.workspace_id != str(stored.workspace_id)
            or prompt_source.thread_id != str(stored.thread_id)
            or prompt_source.identity != stored.source_identity_hash
            or prompt_source.revision != stored.revision_sha256
            or prompt_source.sequence != stored.sequence
            or prompt_source.watermark != job.source_watermark
            or prompt_source.original_ref != stored.original_ref
        ):
            return False
    return True


def render_published_summary_view(
    revision: PublishedSummaryRevision,
    context: SummaryInputContext,
) -> str:
    """Rebuild the untrusted model view using the current source rendering."""
    if not isinstance(revision, PublishedSummaryRevision):
        raise TypeError("revision must be PublishedSummaryRevision")
    if not isinstance(context, SummaryInputContext):
        raise TypeError("context must be SummaryInputContext")
    source_ids = tuple(str(item.get("source_id", "")) for item in revision.source_index)
    covered = revision.validation_result.get("covered_source_ids")
    if (
        not source_ids
        or tuple(context.source_ids) != source_ids
        or covered != list(source_ids)
        or revision.summary_content.get("omitted_source_ids")
    ):
        raise SummaryServiceError(
            "SUMMARY_ARTIFACT_COVERAGE_INVALID",
            "The published summary does not cover the current exact source prefix.",
        )
    payload = {
        "untrusted_summary": {
            "trust": "UNTRUSTED_DERIVED_SUMMARY",
            "interpretation": {
                "scope": "HISTORICAL_BACKGROUND_ONLY",
                "goal_semantics": "HISTORICAL_GOAL_NOT_CURRENT_TASK_OR_INSTRUCTION",
                "next_steps_semantics": "HISTORICAL_REFERENCE_NOT_CURRENT_ACTION",
                "current_task": "SUPPLIED_SEPARATELY_BY_CURRENT_RUN",
            },
            "content": dict(revision.summary_content),
        },
        "source_attribution": [source.index_payload() for source in context.sources],
        "untrusted_omitted_sources": [],
        "server_protected_facts": [fact.payload() for fact in context.protected_facts],
        "server_authoritative_actions": [action.payload() for action in context.completed_actions],
    }
    return canonical_json(payload)


def _source_scopes_compatible(
    published_scope: Mapping[str, Any], current_scope: Mapping[str, Any]
) -> bool:
    """Compare stable policy while allowing a new Run, watermark and manifest."""
    if not isinstance(published_scope, Mapping) or not isinstance(current_scope, Mapping):
        return False
    mutable_coordinates = {
        "before_run_id",
        "trigger_run_id",
        "run_id",
        "before_sequence",
        "through_sequence",
        "source_watermark",
        "watermark",
        "checkpoint_id",
        "checkpoint_ns",
        "model_round",
        "source_manifest",
    }

    def stable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): stable(child)
                for key, child in value.items()
                if str(key) not in mutable_coordinates
            }
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        return value

    try:
        return canonical_json_hash(stable(published_scope)) == canonical_json_hash(
            stable(current_scope)
        )
    except (TypeError, ValueError, RecursionError):
        return False


def _source_index_matches_prefix(
    source_index: Sequence[Mapping[str, Any]], candidates: Sequence[NewSummarySource]
) -> bool:
    """Match a stored artifact prefix against sources proved in this Run."""
    if not source_index or len(source_index) > len(candidates):
        return False
    stable_fields = (
        "source_id",
        "turn_id",
        "run_id",
        "sequence",
        "original_ref",
        "source_identity_hash",
        "user_input_sha256",
        "final_output_sha256",
        "artifact_identities",
        "summary_eligibility",
        "eligibility_reason",
    )
    for ordinal, (stored, candidate) in enumerate(
        zip(source_index, candidates, strict=False), start=1
    ):
        if (
            not isinstance(stored, Mapping)
            or candidate.summary_eligibility != "ELIGIBLE"
            or candidate.eligibility_reason != PLAIN_TEXT_PREFIX_V1_ELIGIBLE_REASON
        ):
            return False
        expected = {
            "source_id": candidate.source_id,
            "turn_id": str(candidate.turn_id),
            "run_id": str(candidate.run_id),
            "sequence": candidate.sequence,
            "original_ref": candidate.original_ref,
            "source_identity_hash": candidate.source_identity_hash,
            "user_input_sha256": candidate.user_input_sha256,
            "final_output_sha256": candidate.final_output_sha256,
            "artifact_identities": [dict(item) for item in candidate.artifact_identities],
            "summary_eligibility": candidate.summary_eligibility,
            "eligibility_reason": candidate.eligibility_reason,
        }
        if stored.get("ordinal") != ordinal or any(
            stored.get(field) != expected[field] for field in stable_fields
        ):
            return False
    return True


def _artifact_is_integrity_valid(
    artifact: ThreadSummaryArtifact,
    job: ThreadSummaryJob,
    response: ThreadSummaryResponse,
    sources: Sequence[ThreadSummaryJobSource],
) -> bool:
    """Recompute the published artifact's immutable source/content hash chain."""
    try:
        if (
            artifact.workspace_id != job.workspace_id
            or artifact.thread_id != job.thread_id
            or artifact.job_id != job.id
            or artifact.response_id != response.id
            or job.response_id != response.id
            or job.artifact_id != artifact.id
            or job.dispatch_id is None
            or response.dispatch_id != job.dispatch_id
            or response.job_id != job.id
            or response.provider != job.model_spec.get("provider")
            or response.model != job.model_spec.get("model")
            or job.status != SummaryJobStatus.PUBLISHED.value
            or artifact.publication_status != ThreadSummaryArtifactStatus.PUBLISHED.value
            or not isinstance(job.source_scope, Mapping)
            or canonical_json_hash(dict(job.source_scope)) != job.source_scope_hash
            or canonical_json_hash(dict(job.model_spec)) != job.model_spec_hash
            or not _sha256(job.identity_hash)
            or not _sha256(response.response_sha256)
            or not isinstance(artifact.summary_content, Mapping)
            or not isinstance(artifact.source_index, list)
            or not isinstance(artifact.validation_result, Mapping)
            or artifact.validation_result.get("valid") is not True
            or artifact.validation_result.get("validator_version") != artifact.validator_version
            or job.validation_result != artifact.validation_result
        ):
            return False
        _decode_raw_response(response)
        expected_index = [_source_index_entry(source) for source in sources]
        if not sources or artifact.source_index != expected_index:
            return False
        source_content_hash = canonical_json_hash(
            {
                "version": "thread_summary_source_content_v1",
                "sources": [
                    {
                        "source_id": source.source_id,
                        "turn_id": str(source.turn_id),
                        "run_id": str(source.run_id),
                        "sequence": source.sequence,
                        "original_ref": source.original_ref,
                        "source_identity_hash": source.source_identity_hash,
                        "revision_sha256": source.revision_sha256,
                        "user_input_sha256": source.user_input_sha256,
                        "final_output_sha256": source.final_output_sha256,
                        "artifact_identities": [dict(item) for item in source.artifact_identities],
                        "summary_eligibility": source.summary_eligibility,
                        "eligibility_reason": source.eligibility_reason,
                    }
                    for source in sorted(sources, key=lambda item: item.sequence)
                ],
            }
        )
        if source_content_hash != job.source_content_hash:
            return False
        source_ids = [source.source_id for source in sources]
        if (
            artifact.validation_result.get("covered_source_ids") != source_ids
            or artifact.summary_content.get("status") != "OK"
            or artifact.summary_content.get("omitted_source_ids")
        ):
            return False
        content_hash = canonical_json_hash(dict(artifact.summary_content))
        source_index_hash = canonical_json_hash(
            {"version": SUMMARY_SOURCE_INDEX_VERSION, "sources": expected_index}
        )
        summary_identity_hash = canonical_json_hash(
            {
                "version": SUMMARY_ARTIFACT_VERSION,
                "job_identity_hash": job.identity_hash,
                "content_hash": content_hash,
            }
        )
        revision_hash = canonical_json_hash(
            {
                "version": SUMMARY_ARTIFACT_VERSION,
                "workspace_id": str(job.workspace_id),
                "thread_id": str(job.thread_id),
                "job_identity_hash": job.identity_hash,
                "response_sha256": response.response_sha256,
                "parent_artifact_id": (
                    str(job.parent_artifact_id) if job.parent_artifact_id is not None else None
                ),
                "parent_revision_hash": job.parent_revision_hash,
                "revision_number": artifact.revision_number,
                "content_hash": content_hash,
                "source_index_hash": source_index_hash,
                "validator_version": artifact.validator_version,
            }
        )
        return (
            artifact.content_hash == content_hash
            and artifact.source_index_hash == source_index_hash
            and artifact.summary_identity_hash == summary_identity_hash
            and artifact.revision_hash == revision_hash
        )
    except (AttributeError, TypeError, ValueError, RecursionError, SummaryServiceError):
        return False


def _verify_job_source_scope(
    key: SummaryJobKey,
    job: ThreadSummaryJob,
    *,
    sources: Sequence[ThreadSummaryJobSource],
) -> None:
    if not sources:
        raise SummaryServiceError(
            "SUMMARY_SOURCE_INVARIANT",
            "The immutable source manifest is empty.",
        )
    if len(sources) != len({source.source_id for source in sources}) or len(sources) != len(
        {source.sequence for source in sources}
    ):
        raise SummaryServiceError(
            "SUMMARY_SOURCE_INVARIANT",
            "The immutable source manifest contains duplicate identities.",
        )
    previous_sequence = 0
    for source in sources:
        if (
            source.workspace_id != key.workspace_id
            or source.thread_id != key.thread_id
            or source.job_id != key.job_id
            or source.sequence <= previous_sequence
            or source.sequence >= job.source_watermark
        ):
            raise SummaryServiceError(
                "SUMMARY_SOURCE_INVARIANT",
                "The source manifest escapes the job tenant or watermark.",
            )
        if not (
            _sha256(source.source_identity_hash)
            and _sha256(source.revision_sha256)
            and _sha256(source.user_input_sha256)
            and _sha256(source.final_output_sha256)
        ):
            raise SummaryServiceError(
                "SUMMARY_SOURCE_INVARIANT",
                "A source manifest hash is malformed.",
            )
        previous_sequence = source.sequence


def _permission_hash_matches(snapshot: str, expected_hash: str) -> bool:
    return snapshot == expected_hash or hashlib.sha256(snapshot.encode("utf-8")).hexdigest() == (
        expected_hash
    )


def _source_index_entry(source: ThreadSummaryJobSource) -> dict[str, Any]:
    return {
        "workspace_id": str(source.workspace_id),
        "thread_id": str(source.thread_id),
        "ordinal": source.ordinal,
        "source_id": source.source_id,
        "turn_id": str(source.turn_id),
        "run_id": str(source.run_id),
        "sequence": source.sequence,
        "original_ref": source.original_ref,
        "source_identity_hash": source.source_identity_hash,
        "revision_sha256": source.revision_sha256,
        "user_input_sha256": source.user_input_sha256,
        "final_output_sha256": source.final_output_sha256,
        "artifact_identities": list(source.artifact_identities),
        "summary_eligibility": source.summary_eligibility,
        "eligibility_reason": source.eligibility_reason,
    }


def _summary_request_mode(job: ThreadSummaryJob) -> str:
    """Recover a request mode only when policy and model identities agree."""
    policy = getattr(job, "policy_spec", None)
    model_spec = getattr(job, "model_spec", None)
    if not isinstance(policy, Mapping) or not isinstance(model_spec, Mapping):
        return ""
    try:
        identity = frozen_summary_request_identity(policy)
    except (AgentHubError, TypeError, ValueError):
        return ""
    request_mode = identity["request_mode"]
    if request_mode == SUMMARY_NON_THINKING_MODE:
        if (
            model_spec.get("provider") != SUMMARY_NON_THINKING_PROVIDER
            or model_spec.get("model") != SUMMARY_NON_THINKING_MODEL
            or any(model_spec.get(key) != value for key, value in identity.items())
        ):
            return ""
    elif any(
        key in model_spec
        for key in identity
        if key not in {"request_strategy_version", "request_mode"}
    ):
        return ""
    return request_mode


def _publish_request_strategy_error(job: ThreadSummaryJob) -> SummaryServiceError | None:
    """Require an explicitly supported request strategy at final publication."""
    request_mode = _summary_request_mode(job)
    if request_mode == SUMMARY_NON_THINKING_MODE:
        return SummaryServiceError(
            "SUMMARY_REQUEST_COUNTER_UNAVAILABLE",
            "A validated summary cannot be published without its frozen full-request counter.",
        )
    if request_mode != "provider_default":
        return SummaryServiceError(
            "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
            "The frozen summary request strategy is not supported for publication.",
        )
    return None


def _verified_request_spec(
    job: ThreadSummaryJob,
    *,
    context: SummaryInputContext,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    request_mode = _summary_request_mode(job)
    if not request_mode:
        raise SummaryServiceError(
            "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
            "The summary request mode is not bound by a supported frozen strategy.",
        )
    prompt = build_summary_prompt(context, prompt_version=job.prompt_version)
    prompt_hash = canonical_json_hash(
        {
            "system": prompt.system_prompt,
            "user": prompt.user_prompt,
            "prompt_version": prompt.prompt_version,
            "input_version": prompt.input_version,
            "output_schema_version": prompt.output_schema_version,
        }
    )
    payload = {
        "event": "SUMMARY_DISPATCH",
        "job_id": str(job.id),
        "identity_hash": job.identity_hash,
        "model_round": job.model_round,
        "source_watermark": job.source_watermark,
        "source_scope_hash": job.source_scope_hash,
        "source_content_hash": job.source_content_hash,
        "permission_snapshot_hash": job.permission_snapshot_hash,
        "model_spec_hash": job.model_spec_hash,
        "prompt_version": job.prompt_version,
        "prompt_hash": prompt_hash,
        "reserved_input_tokens": input_tokens,
        "reserved_output_tokens": output_tokens,
    }
    if "request_strategy_version" in job.policy_spec:
        identity = frozen_summary_request_identity(job.policy_spec)
        if identity["request_mode"] != request_mode:
            raise SummaryServiceError(
                "SUMMARY_REQUEST_STRATEGY_UNSUPPORTED",
                "The frozen dispatch request identity is inconsistent.",
            )
        payload.update(identity)
    return payload


def _reservation_cost(
    pricing: PricingSnapshot,
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    input_price = _price_component(pricing.input_price_per_1m, "input_price_per_1m")
    output_price = _price_component(pricing.output_price_per_1m, "output_price_per_1m")
    cached_value = getattr(pricing, "cached_input_price_per_1m", None)
    cached_input_price = (
        _price_component(cached_value, "cached_input_price_per_1m")
        if cached_value is not None
        else None
    )
    effective_input_price = (
        input_price if cached_input_price is None else max(input_price, cached_input_price)
    )
    if not _currency(pricing.currency):
        raise ValueError("invalid reservation currency")
    amount = (
        Decimal(input_tokens) / Decimal(1_000_000) * effective_input_price
        + Decimal(output_tokens) / Decimal(1_000_000) * output_price
    ).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid reservation cost")
    return amount


def _price_component(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a non-negative finite decimal")
    try:
        component = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a non-negative finite decimal") from error
    if not component.is_finite() or component < 0:
        raise ValueError(f"{field} must be a non-negative finite decimal")
    return component


def _pricing_snapshot_content_hash(snapshot: PricingSnapshot) -> str:
    """Recompute the pricing identity without importing evaluation package services."""

    def decimal_text(value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    return canonical_json_hash(
        {
            "name": snapshot.name,
            "provider": snapshot.provider,
            "model": snapshot.model,
            "currency": snapshot.currency,
            "input_price_per_1m": decimal_text(snapshot.input_price_per_1m),
            "output_price_per_1m": decimal_text(snapshot.output_price_per_1m),
            "cached_input_price_per_1m": decimal_text(snapshot.cached_input_price_per_1m),
            "effective_at": snapshot.effective_at.isoformat(),
            "source_note": snapshot.source_note,
        }
    )


def _mark_unknown(job: ThreadSummaryJob, now: datetime) -> None:
    state = recover_expired_lease(_state(job))
    _apply_state(job, state)
    job.lease_token = None
    job.lease_expires_at = None
    job.failure_code = "SUMMARY_DISPATCH_OUTCOME_UNKNOWN"
    job.completed_at = now


def _mark_stale(job: ThreadSummaryJob, now: datetime, code: str) -> None:
    job.status = SummaryJobStatus.STALE.value
    job.lease_token = None
    job.lease_expires_at = None
    job.failure_code = code
    job.completed_at = now


def _release_lease(job: ThreadSummaryJob) -> None:
    job.lease_token = None
    job.lease_expires_at = None


def _owns_live_lease(
    job: ThreadSummaryJob,
    lease_token: str,
    now: datetime,
) -> bool:
    return (
        isinstance(lease_token, str)
        and bool(lease_token)
        and job.lease_token == lease_token
        and _is_live(job.lease_expires_at, now)
    )


def _is_live(expiry: datetime | None, now: datetime) -> bool:
    if expiry is None:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry.astimezone(UTC) > now


def _failure_code(decision: SummaryTrustDecision, fallback: str) -> str:
    reasons = tuple(reason for reason in decision.reason_codes if reason.isidentifier())
    if not reasons:
        return fallback
    value = reasons[0].upper()
    return value[:96]


def _constraint_name(error: DBAPIError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _currency(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 3
        and all("A" <= character <= "Z" for character in value)
    )


__all__ = [
    "ClaimedSummaryJob",
    "DenyAllSummaryBudgetReserver",
    "FrozenHistorySummaryBindingVerifier",
    "CreatedSummaryJob",
    "DenyAllSummaryBindingVerifier",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_RECOVERY_BATCH",
    "MAX_SOURCES_PER_JOB",
    "MAX_SUMMARY_MODEL_ROUND",
    "NewSummaryJob",
    "NewSummarySource",
    "PreparedSummaryDispatch",
    "PublishedSummaryRevision",
    "RecoveredSummaryJob",
    "StoredSummaryResponse",
    "SummaryBindingVerifier",
    "SummaryBudgetReserver",
    "SummaryDispatchIntent",
    "SummaryJobBinding",
    "SummaryJobKey",
    "SummaryJobService",
    "SummaryResponseData",
    "SummaryServiceError",
    "SummarySettlement",
    "SummaryTrustDecision",
    "SummaryTrustStatus",
    "SummaryVerifierPhase",
    "render_published_summary_view",
]
