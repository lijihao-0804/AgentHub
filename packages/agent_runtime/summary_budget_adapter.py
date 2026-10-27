"""Transaction bridge from summary dispatch intents to the shared Run ledger.

This adapter performs database work only.  It must remain absent from product
composition until the main model, every retry/fallback attempt and every
auxiliary request use the same Run account.  ``SummaryJobService`` keeps its
current DenyAll budget default until that integration exists.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAttemptKind,
    RunBudgetLedger,
    RunBudgetLedgerError,
    RunBudgetReservationBasis,
)
from packages.agent_runtime.summary_service import SummaryJobBinding

_EXPECTED_REJECTION_CODES = frozenset(
    {
        "RUN_BUDGET_ACCOUNT_NOT_FOUND",
        "RUN_BUDGET_ATTEMPT_CONFLICT",
        "RUN_BUDGET_CONSERVATIVE_MAXIMUM_REQUIRED",
        "RUN_BUDGET_CURRENCY_MISMATCH",
        "RUN_BUDGET_INSUFFICIENT_FUNDS",
    }
)


class SummaryRunBudgetReserver:
    """Reserve summary funds in the caller's existing AsyncSession transaction.

    The class structurally implements ``SummaryBudgetReserver``.  It makes no
    provider calls and never commits.  Product code may inject it only after all
    other model and auxiliary attempts are bound to the same Run account.
    """

    def __init__(self, ledger: RunBudgetLedger | None = None) -> None:
        self._ledger = ledger or RunBudgetLedger()

    async def reserve(
        self,
        session: AsyncSession,
        *,
        binding: SummaryJobBinding,
        reservation_id: UUID,
        amount: Decimal,
        currency: str,
    ) -> bool:
        """Return true only when this call creates a new ledger reservation.

        The caller must own the transaction and later commit it with the summary
        dispatch intent.  A duplicate identity, missing account, currency
        mismatch, insufficient funds or unavailable conservative basis returns
        false without committing or starting compensation.  Unexpected ledger
        invariant failures propagate to avoid hiding corrupt accounting state.
        """

        try:
            reservation = await self._ledger.reserve(
                session,
                workspace_id=binding.workspace_id,
                run_id=binding.trigger_run_id,
                attempt_identity=f"summary:{reservation_id}",
                attempt_kind=RunBudgetAttemptKind.SUMMARY,
                amount=amount,
                currency=currency,
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )
        except RunBudgetLedgerError as error:
            if error.code in _EXPECTED_REJECTION_CODES:
                return False
            raise
        return reservation.created is True


__all__ = ["SummaryRunBudgetReserver"]
