"""Durable approval domain for M5 action execution."""

from typing import TYPE_CHECKING

from packages.approvals.contracts import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
    ApprovalTransitionError,
    canonicalize_arguments,
    compute_logical_action_id,
)
from packages.approvals.models import Approval

if TYPE_CHECKING:
    from packages.approvals.reconciliation import ApprovalReconciliationService
    from packages.approvals.service import ApprovalService


def __getattr__(name: str):
    # Metrics and tables import the domain models during worker startup.
    # Loading them must not construct a dependency on the tool/graph services.
    if name == "ApprovalService":
        from packages.approvals.service import ApprovalService

        return ApprovalService
    if name == "ApprovalReconciliationService":
        from packages.approvals.reconciliation import ApprovalReconciliationService

        return ApprovalReconciliationService
    raise AttributeError(name)


__all__ = [
    "Approval",
    "ApprovalDecisionStatus",
    "ApprovalExecutionStatus",
    "ApprovalService",
    "ApprovalReconciliationService",
    "ApprovalTransitionError",
    "canonicalize_arguments",
    "compute_logical_action_id",
]
