"""Provider-neutral Approval contracts and deterministic action identity."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid5

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from packages.core.canonical.json_hash import canonical_json_hash


class ApprovalDecisionStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ApprovalExecutionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class ApprovalTransitionError(ValueError):
    """Raised when a decision or execution state transition is not legal."""


_RESERVED_ARGUMENT_KEYS = frozenset(
    {
        "workspace_id",
        "organization_id",
        "user_id",
        "requested_by",
        "decided_by",
        "approver",
        "logical_action_id",
        "idempotency_key",
        "execution_status",
        "decision_status",
    }
)


def canonicalize_arguments(
    arguments: Mapping[str, Any], input_schema: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """Validate and freeze business arguments before an Approval is created."""

    if not isinstance(arguments, Mapping):
        raise ValueError("tool arguments must be an object")
    if any(key in _RESERVED_ARGUMENT_KEYS for key in arguments):
        raise ValueError("tool arguments contain reserved internal fields")
    try:
        Draft202012Validator.check_schema(dict(input_schema))
        validator = Draft202012Validator(dict(input_schema))
        validator.validate(arguments)
    except (SchemaError, ValidationError) as exc:
        raise ValueError("tool arguments do not match the published input schema") from exc
    normalized = deepcopy(dict(arguments))
    # Round-trip through JSON ensures that the persisted projection is JSON-safe and
    # removes custom Mapping implementations before hashing or persistence.
    try:
        normalized = json.loads(
            json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments are not JSON serializable") from exc
    if not isinstance(normalized, dict):
        raise ValueError("tool arguments must be an object")
    return normalized, canonical_json_hash(normalized)


def compute_logical_action_id(
    *,
    workspace_id: UUID | str,
    run_id: UUID | str,
    tool_revision_id: UUID | str | None,
    canonical_args_hash: str,
    proposal_ordinal: int,
) -> str:
    """Return a deterministic identity independent of provider tool-call IDs."""

    if proposal_ordinal < 0:
        raise ValueError("proposal ordinal must be non-negative")
    identity = {
        "workspace_id": str(workspace_id),
        "run_id": str(run_id),
        "tool_revision_id": str(tool_revision_id) if tool_revision_id is not None else None,
        "canonical_args_hash": canonical_args_hash,
        "proposal_ordinal": proposal_ordinal,
    }
    return str(uuid5(UUID("7f2e2f1e-0f1b-5df3-9d8f-5a3bbf1b3c31"), canonical_json_hash(identity)))


def transition_decision(
    current: ApprovalDecisionStatus, target: ApprovalDecisionStatus
) -> ApprovalDecisionStatus:
    allowed = {
        ApprovalDecisionStatus.PENDING: {
            ApprovalDecisionStatus.APPROVED,
            ApprovalDecisionStatus.DENIED,
            ApprovalDecisionStatus.EXPIRED,
            ApprovalDecisionStatus.CANCELLED,
        }
    }
    if target not in allowed.get(current, set()):
        raise ApprovalTransitionError(f"illegal decision transition: {current} -> {target}")
    return target


def transition_execution(
    current: ApprovalExecutionStatus, target: ApprovalExecutionStatus
) -> ApprovalExecutionStatus:
    allowed = {
        ApprovalExecutionStatus.NOT_STARTED: {
            ApprovalExecutionStatus.CLAIMED,
            ApprovalExecutionStatus.FAILED,
            ApprovalExecutionStatus.UNKNOWN_OUTCOME,
        },
        ApprovalExecutionStatus.CLAIMED: {
            ApprovalExecutionStatus.SUCCEEDED,
            ApprovalExecutionStatus.FAILED,
            ApprovalExecutionStatus.UNKNOWN_OUTCOME,
        },
    }
    if target not in allowed.get(current, set()):
        raise ApprovalTransitionError(f"illegal execution transition: {current} -> {target}")
    return target


__all__ = [
    "ApprovalDecisionStatus",
    "ApprovalExecutionStatus",
    "ApprovalTransitionError",
    "canonicalize_arguments",
    "compute_logical_action_id",
    "transition_decision",
    "transition_execution",
]
