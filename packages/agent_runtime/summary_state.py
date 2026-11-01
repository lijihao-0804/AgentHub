"""Pure transitions for a durable, single-dispatch thread summary job.

Persistence must apply each transition under the job row lock and commit the
dispatch intent and budget reservation together before any provider call.
An expired lease after intent is ambiguous and never authorizes a resend.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class SummaryJobStatus(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    DISPATCH_INTENT = "DISPATCH_INTENT"
    RESPONSE_STORED = "RESPONSE_STORED"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    STALE = "STALE"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class SummaryTransitionError(ValueError):
    """The requested transition is not safe for the persisted state."""


@dataclass(frozen=True, slots=True)
class SummaryState:
    status: SummaryJobStatus = SummaryJobStatus.QUEUED
    lease_token: str | None = None
    budget_reserved: bool = False
    response_ref: str | None = None
    revision_ref: str | None = None


def claim(state: SummaryState, *, lease_token: str) -> SummaryState:
    if state.status is not SummaryJobStatus.QUEUED or not _valid_token(lease_token):
        raise SummaryTransitionError("Only a queued job may be claimed.")
    return replace(state, status=SummaryJobStatus.CLAIMED, lease_token=lease_token)


def commit_dispatch_intent(state: SummaryState, *, lease_token: str) -> SummaryState:
    _require_owner(state, lease_token, SummaryJobStatus.CLAIMED)
    # The persistence adapter commits this state and its reservation atomically.
    return replace(state, status=SummaryJobStatus.DISPATCH_INTENT, budget_reserved=True)


def fail_before_dispatch(state: SummaryState, *, lease_token: str) -> SummaryState:
    """Finish a local rejection before any dispatch intent has been committed."""
    _require_owner(state, lease_token, SummaryJobStatus.CLAIMED)
    if state.budget_reserved or state.response_ref:
        raise SummaryTransitionError("Dispatch evidence forbids a pre-dispatch failure.")
    return replace(state, status=SummaryJobStatus.FAILED)


def store_response(state: SummaryState, *, lease_token: str, response_ref: str) -> SummaryState:
    _require_owner(state, lease_token, SummaryJobStatus.DISPATCH_INTENT)
    if not state.budget_reserved or not response_ref:
        raise SummaryTransitionError("A response requires a reserved dispatch and reference.")
    return replace(state, status=SummaryJobStatus.RESPONSE_STORED, response_ref=response_ref)


def validate_response(state: SummaryState, *, lease_token: str, valid: bool) -> SummaryState:
    _require_owner(state, lease_token, SummaryJobStatus.RESPONSE_STORED)
    if type(valid) is not bool:
        raise SummaryTransitionError("Validation result must be an explicit bool.")
    if not state.response_ref:
        raise SummaryTransitionError("The provider response must be stored before validation.")
    return replace(state, status=SummaryJobStatus.VALIDATED if valid else SummaryJobStatus.FAILED)


def publish(
    state: SummaryState, *, lease_token: str, revision_ref: str, bindings_current: bool
) -> SummaryState:
    _require_owner(state, lease_token, SummaryJobStatus.VALIDATED)
    if type(bindings_current) is not bool:
        raise SummaryTransitionError("Binding verification must be an explicit bool.")
    if not bindings_current:
        return replace(state, status=SummaryJobStatus.STALE)
    if not revision_ref:
        raise SummaryTransitionError("Publication requires an immutable revision reference.")
    return replace(state, status=SummaryJobStatus.PUBLISHED, revision_ref=revision_ref)


def recover_expired_lease(state: SummaryState) -> SummaryState:
    """Recover only when the caller has established lease expiry under lock."""
    if state.status is SummaryJobStatus.CLAIMED:
        if state.budget_reserved or state.response_ref:
            raise SummaryTransitionError("Claimed state has inconsistent dispatch evidence.")
        return replace(state, status=SummaryJobStatus.QUEUED, lease_token=None)
    if state.status is SummaryJobStatus.DISPATCH_INTENT:
        return replace(state, status=SummaryJobStatus.UNKNOWN_OUTCOME, lease_token=None)
    if state.status in {SummaryJobStatus.RESPONSE_STORED, SummaryJobStatus.VALIDATED}:
        # A fresh lease can resume local validation/publication without dispatch.
        return replace(state, lease_token=None)
    raise SummaryTransitionError("The job is not recoverable through lease expiry.")


def claim_recovery(state: SummaryState, *, lease_token: str) -> SummaryState:
    if (
        state.status not in {SummaryJobStatus.RESPONSE_STORED, SummaryJobStatus.VALIDATED}
        or state.lease_token is not None
        or not _valid_token(lease_token)
    ):
        raise SummaryTransitionError("Only saved response or validation can be recovered.")
    return replace(state, lease_token=lease_token)


def _require_owner(state: SummaryState, lease_token: str, status: SummaryJobStatus) -> None:
    if (
        state.status is not status
        or not _valid_token(lease_token)
        or state.lease_token != lease_token
    ):
        raise SummaryTransitionError("The job state or execution lease is invalid.")


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()
