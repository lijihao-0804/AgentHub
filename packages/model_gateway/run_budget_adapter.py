"""Opt-in Run budget contracts for individual main-model provider attempts.

The gateway protocol is scoped to one logical model invocation inside one Run.
The caller must supply a replay-stable call identity and a trusted source for
the conservative maximum cost.  This module never derives a monetary upper
bound from token counts, provider output, or a response estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.contracts import ModelRequest
from packages.model_gateway.profile_resolution import ResolvedModelProfile


class ModelAttemptKind(StrEnum):
    """The provider-call class used for a main-model attempt."""

    MAIN_MODEL = "MAIN_MODEL"
    RETRY = "RETRY"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True, slots=True)
class AttemptCostUpperBound:
    """A trusted outward price assertion for one provider dispatch.

    ``basis_identity`` must identify the frozen price/request evidence that
    supports the whole amount.  The Run ledger currently has no column for this
    identity, so implementations must reject missing evidence but cannot
    persist or independently verify it.
    """

    amount: Decimal
    currency: str
    basis_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if not self.amount.is_finite() or self.amount.is_signed() or self.amount == 0:
            raise ValueError("amount must be a positive finite Decimal")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or any(not "A" <= character <= "Z" for character in self.currency)
        ):
            raise ValueError("currency must be exactly three uppercase ASCII letters")
        if (
            not isinstance(self.basis_identity, str)
            or not self.basis_identity
            or self.basis_identity != self.basis_identity.strip()
            or len(self.basis_identity) > 256
            or "\x00" in self.basis_identity
        ):
            raise ValueError("basis_identity must be non-empty without surrounding whitespace")


class ModelAttemptPriceSource(Protocol):
    """Resolve a trusted conservative maximum inside the reservation session."""

    async def resolve(
        self,
        session: AsyncSession,
        *,
        profile: ResolvedModelProfile,
        request: ModelRequest,
        provider: str,
        attempt_kind: ModelAttemptKind,
    ) -> AttemptCostUpperBound | None: ...


@dataclass(frozen=True, slots=True)
class ModelAttemptBudgetReservation:
    """A committed local hold; it is not reconciliation of provider usage."""

    workspace_id: UUID
    run_id: UUID
    attempt_identity: str
    attempt_kind: ModelAttemptKind
    cost_upper_bound: AttemptCostUpperBound


class ModelAttemptBudget(Protocol):
    """Gateway-facing strict budget boundary."""

    async def reserve_attempt(
        self,
        context: WorkspaceExecutionContext,
        profile: ResolvedModelProfile,
        request: ModelRequest,
        *,
        provider: str,
        operation: Literal["generate", "stream"],
        profile_index: int,
        attempt_index: int,
        attempt_kind: ModelAttemptKind,
    ) -> ModelAttemptBudgetReservation: ...

    async def mark_unknown_outcome(
        self,
        reservation: ModelAttemptBudgetReservation,
    ) -> None: ...


class ModelAttemptBudgetError(Exception):
    """A safe strict-mode failure that occurred before or after dispatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = [
    "AttemptCostUpperBound",
    "ModelAttemptBudget",
    "ModelAttemptBudgetError",
    "ModelAttemptBudgetReservation",
    "ModelAttemptKind",
    "ModelAttemptPriceSource",
]
