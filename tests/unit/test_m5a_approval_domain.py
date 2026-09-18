from __future__ import annotations

from uuid import UUID

import pytest

from packages.approvals.contracts import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
    ApprovalTransitionError,
    canonicalize_arguments,
    compute_logical_action_id,
    transition_decision,
    transition_execution,
)


def test_canonical_arguments_are_order_independent_and_hashed() -> None:
    schema = {
        "type": "object",
        "properties": {"subject": {"type": "string"}, "priority": {"type": "string"}},
        "required": ["subject", "priority"],
        "additionalProperties": False,
    }
    first, first_hash = canonicalize_arguments(
        {"priority": "HIGH", "subject": "broken"}, schema
    )
    second, second_hash = canonicalize_arguments(
        {"subject": "broken", "priority": "HIGH"}, schema
    )
    assert first == second
    assert first_hash == second_hash


def test_canonical_arguments_reject_internal_fields_and_schema_errors() -> None:
    schema = {"type": "object", "properties": {"subject": {"type": "string"}}}
    with pytest.raises(ValueError, match="reserved"):
        canonicalize_arguments({"workspace_id": "other", "subject": "x"}, schema)
    with pytest.raises(ValueError):
        canonicalize_arguments({"subject": 42}, schema)


def test_logical_action_id_ignores_provider_call_id() -> None:
    kwargs = {
        "workspace_id": UUID("11111111-1111-1111-1111-111111111111"),
        "run_id": UUID("22222222-2222-2222-2222-222222222222"),
        "tool_revision_id": UUID("33333333-3333-3333-3333-333333333333"),
        "canonical_args_hash": "a" * 64,
        "proposal_ordinal": 0,
    }
    assert compute_logical_action_id(**kwargs) == compute_logical_action_id(**kwargs)


def test_decision_and_execution_state_transitions_fail_closed() -> None:
    assert transition_decision(ApprovalDecisionStatus.PENDING, ApprovalDecisionStatus.APPROVED)
    assert transition_execution(
        ApprovalExecutionStatus.NOT_STARTED, ApprovalExecutionStatus.CLAIMED
    )
    with pytest.raises(ApprovalTransitionError):
        transition_decision(ApprovalDecisionStatus.APPROVED, ApprovalDecisionStatus.DENIED)
    with pytest.raises(ApprovalTransitionError):
        transition_execution(
            ApprovalExecutionStatus.SUCCEEDED, ApprovalExecutionStatus.CLAIMED
        )
