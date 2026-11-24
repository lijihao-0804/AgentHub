"""Contracts, types, and verifier protocols extracted from summary_service.py."""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    SummaryPrompt,
)

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




def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in '0123456789abcdef' for character in value)
    )


def _permission_hash_matches(permission_snapshot: str, expected_hash: str) -> bool:
    actual = hashlib.sha256(permission_snapshot.encode('utf-8')).hexdigest()
    return secrets.compare_digest(actual, expected_hash)
