"""Run-backed implementation of the model-gateway attempt budget protocol."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAttemptKind,
    RunBudgetLedger,
    RunBudgetLedgerError,
    RunBudgetReservationBasis,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.contracts import ModelRequest
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.model_gateway.run_budget_adapter import (
    AttemptCostUpperBound,
    ModelAttemptBudgetError,
    ModelAttemptBudgetReservation,
    ModelAttemptKind,
    ModelAttemptPriceSource,
)


class RunBudgetAttemptAdapter:
    """Commit a Run reservation before every injected provider attempt.

    The reservation transaction commits before this method returns. Provider
    execution then uses no database session. A timeout, cancellation or provider
    error is recorded in a separate transaction; a crash before that transaction
    leaves the conservative hold in ``RESERVED``.

    This bridge has no success-response persistence callback. An in-memory
    response is neither a durable response record nor a provider bill, so its
    arrival must not be described as reconciliation. The reservation therefore
    remains ``RESERVED``; no successful-response persistence or bill settlement
    is implemented here.
    """

    _MAX_CALL_IDENTITY_LENGTH = 64

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        workspace_id: UUID,
        run_id: UUID,
        call_identity: str,
        price_source: ModelAttemptPriceSource,
        ledger: RunBudgetLedger | None = None,
    ) -> None:
        for value, field in (
            (workspace_id, "workspace_id"),
            (run_id, "run_id"),
        ):
            if not isinstance(value, UUID):
                raise TypeError(f"{field} must be a UUID")
        _validate_call_identity(
            call_identity,
            maximum=self._MAX_CALL_IDENTITY_LENGTH,
        )
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._run_id = run_id
        self._call_identity = call_identity
        self._price_source = price_source
        self._ledger = ledger or RunBudgetLedger()

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
    ) -> ModelAttemptBudgetReservation:
        """Create and commit one fresh reservation before provider dispatch."""

        self._validate_context(context)
        _validate_index(profile_index, "profile_index")
        _validate_index(attempt_index, "attempt_index")
        if operation not in {"generate", "stream"}:
            raise TypeError("operation must be generate or stream")
        if not isinstance(attempt_kind, ModelAttemptKind):
            raise TypeError("attempt_kind must be a ModelAttemptKind")
        if not isinstance(provider, str) or not provider:
            raise TypeError("provider must be a non-empty string")

        attempt_identity = (
            f"model:{self._call_identity}:{operation}:{profile_index}:{attempt_index}"
        )
        if len(attempt_identity) > 128:
            raise ModelAttemptBudgetError(
                "RUN_BUDGET_ATTEMPT_IDENTITY_INVALID",
                "The Run budget attempt identity exceeds the ledger limit.",
            )

        ledger_kind = RunBudgetAttemptKind(attempt_kind.value)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    quote = await self._price_source.resolve(
                        session,
                        profile=profile,
                        request=request,
                        provider=provider,
                        attempt_kind=attempt_kind,
                    )
                    if quote is None:
                        raise ModelAttemptBudgetError(
                            "RUN_BUDGET_UPPER_BOUND_UNAVAILABLE",
                            "No trusted conservative maximum cost is available.",
                        )
                    if not isinstance(quote, AttemptCostUpperBound):
                        raise ModelAttemptBudgetError(
                            "RUN_BUDGET_UPPER_BOUND_INVALID",
                            "The trusted cost source returned an invalid upper bound.",
                        )
                    reservation = await self._ledger.reserve(
                        session,
                        workspace_id=self._workspace_id,
                        run_id=self._run_id,
                        attempt_identity=attempt_identity,
                        attempt_kind=ledger_kind,
                        amount=quote.amount,
                        currency=quote.currency,
                        reservation_basis=(RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM),
                    )
                    if reservation.created is not True:
                        raise ModelAttemptBudgetError(
                            "RUN_BUDGET_ATTEMPT_REPLAYED",
                            "The stable attempt identity already exists; "
                            "the provider was not called.",
                        )
        except ModelAttemptBudgetError:
            raise
        except RunBudgetLedgerError as error:
            raise ModelAttemptBudgetError(error.code, error.message) from None
        except Exception:
            raise ModelAttemptBudgetError(
                "RUN_BUDGET_RESERVATION_FAILED",
                "The Run budget reservation could not be committed.",
            ) from None

        return ModelAttemptBudgetReservation(
            workspace_id=self._workspace_id,
            run_id=self._run_id,
            attempt_identity=attempt_identity,
            attempt_kind=attempt_kind,
            cost_upper_bound=quote,
        )

    async def mark_unknown_outcome(
        self,
        reservation: ModelAttemptBudgetReservation,
    ) -> None:
        """Retain a dispatched attempt whose provider outcome is uncertain."""

        if not isinstance(reservation, ModelAttemptBudgetReservation):
            raise TypeError("reservation must be a ModelAttemptBudgetReservation")
        if reservation.workspace_id != self._workspace_id or reservation.run_id != self._run_id:
            raise ModelAttemptBudgetError(
                "RUN_BUDGET_BINDING_MISMATCH",
                "The reservation belongs to a different Run budget binding.",
            )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._ledger.mark_unknown_outcome(
                        session,
                        workspace_id=reservation.workspace_id,
                        run_id=reservation.run_id,
                        attempt_identity=reservation.attempt_identity,
                    )
        except RunBudgetLedgerError as error:
            raise ModelAttemptBudgetError(error.code, error.message) from None
        except Exception:
            raise ModelAttemptBudgetError(
                "RUN_BUDGET_UNKNOWN_OUTCOME_FAILED",
                "The unknown provider outcome could not be persisted.",
            ) from None

    def _validate_context(self, context: WorkspaceExecutionContext) -> None:
        try:
            workspace_id = UUID(context.workspace_id)
        except (TypeError, ValueError):
            workspace_id = None
        if workspace_id != self._workspace_id:
            raise ModelAttemptBudgetError(
                "RUN_BUDGET_BINDING_MISMATCH",
                "The request context belongs to a different Workspace.",
            )


def _validate_call_identity(value: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(
            f"call_identity must be non-empty, trimmed and at most {maximum} characters"
        )


def _validate_index(value: int, field: str) -> None:
    if type(value) is not int or value < 0:
        raise TypeError(f"{field} must be a non-negative integer")


__all__ = ["RunBudgetAttemptAdapter"]
