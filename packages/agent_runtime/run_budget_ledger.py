"""Durable PostgreSQL budget reservation primitives for one agent Run.

This module is deliberately not wired into the runtime or model gateway.  It
provides the persistence contract future integrations can use before each
provider attempt.  A local reservation cannot prove that a remote provider
accepted a request exactly once, and this primitive makes no such claim.
It does not change the summary budget adapter's deny-by-default integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    case,
    func,
    select,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from packages.agent_runtime.models import AgentRun
from packages.core.database import Base

AMOUNT_PRECISION = 20
AMOUNT_SCALE = 8
MAX_AMOUNT = Decimal("999999999999.99999999")
MAX_ATTEMPT_IDENTITY_LENGTH = 128
MAX_EVIDENCE_IDENTITY_LENGTH = 256


class RunBudgetAttemptKind(StrEnum):
    """The provider-call class sharing the Run account."""

    MAIN_MODEL = "MAIN_MODEL"
    RETRY = "RETRY"
    FALLBACK = "FALLBACK"
    SUMMARY = "SUMMARY"
    AUXILIARY = "AUXILIARY"


class RunBudgetReservationBasis(StrEnum):
    """Declared basis for a prospective dispatch reservation.

    ``ESTIMATE`` remains for historical/response accounting but cannot obtain a
    new reservation.  ``CONSERVATIVE_MAXIMUM`` is the only accepted dispatch
    basis.  That value is a trusted upstream assertion: this ledger cannot prove
    that the supplied pricing and complete request inputs actually form a
    worst-case upper bound.
    """

    ESTIMATE = "ESTIMATE"
    CONSERVATIVE_MAXIMUM = "CONSERVATIVE_MAXIMUM"


class RunBudgetAttemptStatus(StrEnum):
    """Durable accounting state, independent of provider execution state."""

    RESERVED = "RESERVED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    SETTLED = "SETTLED"


class RunBudgetSettlementEvidence(StrEnum):
    """Evidence retained for future reconciliation.

    Only ``PROVIDER_BILL`` is currently accepted as final settlement.  The
    estimate values remain in the enum for response-side accounting, but this
    ledger rejects them as final settlement until their semantics are wired and
    validated separately.
    """

    RESPONSE_ESTIMATE = "RESPONSE_ESTIMATE"
    CONSERVATIVE_CHARGE = "CONSERVATIVE_CHARGE"
    PROVIDER_BILL = "PROVIDER_BILL"


class RunBudgetLedgerError(Exception):
    """A budget operation was rejected without weakening the account invariant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RunBudgetAccount(Base):
    """One fixed-currency account shared by every attempt in a Run."""

    __tablename__ = "run_budget_accounts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_run_budget_accounts_run_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_run_budget_accounts_currency",
        ),
        CheckConstraint(
            "limit_amount > 0 AND "
            "limit_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_run_budget_accounts_limit_positive_finite",
        ),
        CheckConstraint(
            "reserved_amount >= 0 AND reserved_amount <= limit_amount",
            name="ck_run_budget_accounts_reserved_bounds",
        ),
        CheckConstraint(
            "settled_amount >= 0 AND "
            "settled_amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_run_budget_accounts_settled_bounds",
        ),
        PrimaryKeyConstraint(
            "workspace_id",
            "run_id",
            name="pk_run_budget_accounts",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    reserved_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    settled_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RunBudgetAttempt(Base):
    """An immutable reservation identity with append-only settlement evidence."""

    __tablename__ = "run_budget_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["run_budget_accounts.workspace_id", "run_budget_accounts.run_id"],
            name="fk_run_budget_attempts_account_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_run_budget_attempts_run_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "run_id",
            "attempt_identity",
            name="uq_run_budget_attempts_identity",
        ),
        CheckConstraint(
            "attempt_identity = btrim(attempt_identity) AND "
            "length(attempt_identity) BETWEEN 1 AND 128",
            name="ck_run_budget_attempts_identity",
        ),
        CheckConstraint(
            "attempt_kind IN ('MAIN_MODEL', 'RETRY', 'FALLBACK', 'SUMMARY', 'AUXILIARY')",
            name="ck_run_budget_attempts_kind",
        ),
        CheckConstraint(
            "reserved_amount > 0 AND "
            "reserved_amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_run_budget_attempts_reserved_positive_finite",
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_run_budget_attempts_currency",
        ),
        CheckConstraint(
            "reservation_basis = 'CONSERVATIVE_MAXIMUM'",
            name="ck_run_budget_attempts_reservation_basis",
        ),
        CheckConstraint(
            "status IN ('RESERVED', 'UNKNOWN_OUTCOME', 'SETTLED')",
            name="ck_run_budget_attempts_status",
        ),
        CheckConstraint(
            "settlement_currency IS NULL OR settlement_currency ~ '^[A-Z]{3}$'",
            name="ck_run_budget_attempts_settlement_currency",
        ),
        CheckConstraint(
            "settlement_currency IS NULL OR settlement_currency = currency",
            name="ck_run_budget_attempts_settlement_currency_matches",
        ),
        CheckConstraint(
            "settlement_evidence_kind IS NULL OR settlement_evidence_kind IN "
            "('RESPONSE_ESTIMATE', 'CONSERVATIVE_CHARGE', 'PROVIDER_BILL')",
            name="ck_run_budget_attempts_settlement_evidence_kind",
        ),
        CheckConstraint(
            "settlement_amount IS NULL OR "
            "(settlement_amount >= 0 AND settlement_amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
            name="ck_run_budget_attempts_settlement_amount",
        ),
        CheckConstraint(
            "settlement_evidence_identity IS NULL OR "
            "(settlement_evidence_identity = btrim(settlement_evidence_identity) "
            "AND length(settlement_evidence_identity) BETWEEN 1 AND 256)",
            name="ck_run_budget_attempts_settlement_identity",
        ),
        CheckConstraint(
            "("
            "status = 'SETTLED' AND settlement_amount IS NOT NULL "
            "AND settlement_currency IS NOT NULL "
            "AND settlement_evidence_kind IS NOT NULL "
            "AND settlement_evidence_identity IS NOT NULL "
            "AND settlement_is_estimate IS NOT NULL "
            "AND settled_at IS NOT NULL"
            ") OR ("
            "status IN ('RESERVED', 'UNKNOWN_OUTCOME') "
            "AND settlement_amount IS NULL "
            "AND settlement_currency IS NULL "
            "AND settlement_evidence_kind IS NULL "
            "AND settlement_evidence_identity IS NULL "
            "AND settlement_is_estimate IS NULL "
            "AND settled_at IS NULL"
            ")",
            name="ck_run_budget_attempts_settlement_shape",
        ),
        CheckConstraint(
            "settlement_evidence_kind IS NULL OR "
            "settlement_is_estimate = (settlement_evidence_kind <> 'PROVIDER_BILL')",
            name="ck_run_budget_attempts_settlement_estimate_flag",
        ),
        CheckConstraint(
            "status <> 'SETTLED' OR settlement_evidence_kind = 'PROVIDER_BILL'",
            name="ck_run_budget_attempts_final_settlement_provider_bill",
        ),
        PrimaryKeyConstraint("id", name="pk_run_budget_attempts"),
        Index(
            "ix_run_budget_attempts_workspace_run_status",
            "workspace_id",
            "run_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    attempt_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reservation_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=RunBudgetAttemptStatus.RESERVED.value
    )
    settlement_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    settlement_currency: Mapped[str | None] = mapped_column(String(3))
    settlement_evidence_kind: Mapped[str | None] = mapped_column(String(32))
    settlement_evidence_identity: Mapped[str | None] = mapped_column(String(256))
    settlement_is_estimate: Mapped[bool | None] = mapped_column(Boolean)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


@dataclass(frozen=True, slots=True)
class RunBudgetAccountSnapshot:
    """A point-in-time view of the account within the caller's transaction."""

    workspace_id: UUID
    run_id: UUID
    currency: str
    limit_amount: Decimal
    reserved_amount: Decimal
    settled_amount: Decimal

    @property
    def available_amount(self) -> Decimal:
        return max(
            self.limit_amount - self.reserved_amount - self.settled_amount,
            Decimal("0"),
        )

    @property
    def committed_amount(self) -> Decimal:
        return self.reserved_amount + self.settled_amount

    @property
    def overage_amount(self) -> Decimal:
        return max(self.committed_amount - self.limit_amount, Decimal("0"))

    @property
    def is_over_limit(self) -> bool:
        return self.overage_amount > 0


@dataclass(frozen=True, slots=True)
class RunBudgetAttemptSnapshot:
    """The original reservation and any later evidence-backed settlement."""

    id: UUID
    workspace_id: UUID
    run_id: UUID
    attempt_identity: str
    attempt_kind: RunBudgetAttemptKind
    reserved_amount: Decimal
    currency: str
    reservation_basis: RunBudgetReservationBasis
    status: RunBudgetAttemptStatus
    settlement_amount: Decimal | None
    settlement_currency: str | None
    settlement_evidence: RunBudgetSettlementEvidence | None
    settlement_evidence_identity: str | None
    settlement_is_estimate: bool | None
    settled_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunBudgetReservation:
    """Result of an idempotent reserve operation."""

    account: RunBudgetAccountSnapshot
    attempt: RunBudgetAttemptSnapshot
    created: bool


@dataclass(frozen=True, slots=True)
class RunBudgetSettlement:
    """Result of an idempotent evidence-backed settlement."""

    account: RunBudgetAccountSnapshot
    attempt: RunBudgetAttemptSnapshot
    created: bool


class RunBudgetLedger:
    """Transaction-scoped operations over a Run's shared budget account.

    Every mutating method requires a caller-owned transaction.  The method
    flushes its writes and never commits, so a summary dispatch intent or a
    future runtime step can commit atomically with its reservation.  Callers
    roll back by rolling back that transaction.  Account and attempt lookups
    force ``populate_existing`` so reusing one AsyncSession for a later
    transaction refreshes identity-map objects from PostgreSQL.
    """

    async def create_account(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        currency: str,
        limit_amount: Decimal,
    ) -> tuple[RunBudgetAccountSnapshot, bool]:
        """Create one fixed-currency Run account.

        Precondition: a trusted server-side service selected the Run, currency
        and limit.  This operation neither validates a published AgentVersion
        policy nor claims that the supplied limit came from trusted publishing.
        Repeating the same key and content is idempotent; a different currency
        or limit for the key raises ``RUN_BUDGET_ACCOUNT_CONFLICT``.  A missing
        Run raises ``RUN_BUDGET_RUN_NOT_FOUND``.  The caller owns commit or
        rollback, so a rollback leaves no account behind.
        """

        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(run_id, "run_id")
        _validate_currency(currency)
        normalized_limit = _validate_amount(
            limit_amount,
            field="limit_amount",
            allow_zero=False,
        )

        run_exists = await session.scalar(
            select(AgentRun.id).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.id == run_id,
            )
        )
        if run_exists is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_RUN_NOT_FOUND",
                "The Run does not exist inside the supplied Workspace.",
            )

        statement = (
            postgresql_insert(RunBudgetAccount.__table__)
            .values(
                workspace_id=workspace_id,
                run_id=run_id,
                currency=currency,
                limit_amount=normalized_limit,
                reserved_amount=Decimal("0"),
                settled_amount=Decimal("0"),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    RunBudgetAccount.__table__.c.workspace_id,
                    RunBudgetAccount.__table__.c.run_id,
                ]
            )
            .returning(RunBudgetAccount.__table__.c.run_id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none() is not None
        account = await session.scalar(
            select(RunBudgetAccount)
            .where(
                RunBudgetAccount.workspace_id == workspace_id,
                RunBudgetAccount.run_id == run_id,
            )
            .execution_options(populate_existing=True)
        )
        if account is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_UNAVAILABLE",
                "The Run budget account could not be observed after creation.",
            )
        if account.currency != currency or account.limit_amount != normalized_limit:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_CONFLICT",
                "The Run already has a different fixed budget account.",
            )
        return _account_snapshot(account), inserted

    async def reserve(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        attempt_identity: str,
        attempt_kind: RunBudgetAttemptKind,
        amount: Decimal,
        currency: str,
        reservation_basis: RunBudgetReservationBasis,
    ) -> RunBudgetReservation:
        """Hold the worst-case amount before one provider attempt.

        Precondition: the caller has a stable logical attempt identity that is
        derived from the Run operation rather than a provider-generated call
        ID, and declares ``reservation_basis=CONSERVATIVE_MAXIMUM``.
        ``ESTIMATE`` is retained for historical and response accounting but is
        rejected with ``RUN_BUDGET_CONSERVATIVE_MAXIMUM_REQUIRED`` without
        creating an attempt or changing the account.  The conservative label is
        only a trusted upstream assertion; this module cannot verify that the
        pricing and complete input are a true upper bound.

        The method locks the account, validates precision and currency, and
        rejects an insufficient account with
        ``RUN_BUDGET_INSUFFICIENT_FUNDS``.  A missing account raises
        ``RUN_BUDGET_ACCOUNT_NOT_FOUND`` and a different account currency raises
        ``RUN_BUDGET_CURRENCY_MISMATCH``.  Reusing an identity is idempotent only
        when amount, kind, basis and currency are identical; every other reuse
        raises ``RUN_BUDGET_ATTEMPT_CONFLICT``.  Writes remain in the caller's
        transaction.  This result is a local hold and does not prove that the
        remote provider's call is atomic or exactly-once.
        """

        _validate_budget_key(workspace_id, run_id)
        _validate_attempt_identity(attempt_identity)
        _validate_attempt_kind(attempt_kind)
        normalized_amount = _validate_amount(amount, field="amount", allow_zero=False)
        _validate_currency(currency)
        _validate_reservation_basis(reservation_basis)
        if reservation_basis is not RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_CONSERVATIVE_MAXIMUM_REQUIRED",
                "A dispatch reservation requires a conservative maximum basis.",
            )

        account = await self._lock_account(session, workspace_id, run_id)
        if account.currency != currency:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_CURRENCY_MISMATCH",
                "The reservation currency differs from the Run account currency.",
            )

        existing = await session.scalar(
            select(RunBudgetAttempt)
            .where(
                RunBudgetAttempt.workspace_id == workspace_id,
                RunBudgetAttempt.run_id == run_id,
                RunBudgetAttempt.attempt_identity == attempt_identity,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if existing is not None:
            _assert_matching_reservation(
                existing,
                amount=normalized_amount,
                currency=currency,
                attempt_kind=attempt_kind,
                reservation_basis=reservation_basis,
            )
            return RunBudgetReservation(
                account=_account_snapshot(account),
                attempt=_attempt_snapshot(existing),
                created=False,
            )

        new_reserved = account.reserved_amount + normalized_amount
        if new_reserved + account.settled_amount > account.limit_amount:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_INSUFFICIENT_FUNDS",
                "The Run account cannot fund the full requested reservation.",
            )

        attempt = RunBudgetAttempt(
            id=uuid4(),
            workspace_id=workspace_id,
            run_id=run_id,
            attempt_identity=attempt_identity,
            attempt_kind=attempt_kind.value,
            reserved_amount=normalized_amount,
            currency=currency,
            reservation_basis=reservation_basis.value,
            status=RunBudgetAttemptStatus.RESERVED.value,
        )
        account.reserved_amount = new_reserved
        session.add(attempt)
        # The account guard compares its totals with retained attempt rows.
        await session.flush([attempt])
        await session.flush([account])
        await session.refresh(attempt)
        return RunBudgetReservation(
            account=_account_snapshot(account),
            attempt=_attempt_snapshot(attempt),
            created=True,
        )

    async def mark_unknown_outcome(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        attempt_identity: str,
    ) -> RunBudgetAttemptSnapshot:
        """Mark an attempt's provider outcome unknown without releasing funds.

        Precondition: the caller reached a network boundary or observed a
        timeout/cancellation after reservation and cannot prove that no charge
        occurred.  The full original reservation remains held.  Repeating the
        call for an already-unknown attempt is idempotent.  A missing attempt
        raises ``RUN_BUDGET_ATTEMPT_NOT_FOUND``, a missing account raises
        ``RUN_BUDGET_ACCOUNT_NOT_FOUND``, and a settled attempt raises
        ``RUN_BUDGET_ATTEMPT_STATE_CONFLICT``.  This operation never retries the
        provider call and leaves commit or rollback to the caller.
        """

        _validate_budget_key(workspace_id, run_id)
        _validate_attempt_identity(attempt_identity)
        account = await self._lock_account(session, workspace_id, run_id)
        attempt = await self._lock_attempt(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            attempt_identity=attempt_identity,
        )
        if attempt.status == RunBudgetAttemptStatus.SETTLED.value:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ATTEMPT_STATE_CONFLICT",
                "A settled attempt cannot be changed back to an unknown outcome.",
            )
        if attempt.status == RunBudgetAttemptStatus.RESERVED.value:
            attempt.status = RunBudgetAttemptStatus.UNKNOWN_OUTCOME.value
            await session.flush()
            await session.refresh(attempt)
        elif attempt.status != RunBudgetAttemptStatus.UNKNOWN_OUTCOME.value:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ATTEMPT_STATE_CONFLICT",
                "The attempt has an unsupported budget state.",
            )
        if account.reserved_amount < attempt.reserved_amount:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_INVARIANT",
                "The account does not retain the full unknown-outcome reservation.",
            )
        return _attempt_snapshot(attempt)

    async def settle(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        attempt_identity: str,
        amount: Decimal,
        currency: str,
        evidence: RunBudgetSettlementEvidence,
        evidence_identity: str,
    ) -> RunBudgetSettlement:
        """Post a verified provider bill as the attempt's final settlement.

        Precondition: a trusted caller already obtained and persisted a provider
        bill together with a stable reference to it.  The supplied
        ``evidence_identity`` is only that reference; this method cannot prove
        the referenced bill is real.  Only ``PROVIDER_BILL`` is accepted.
        ``RESPONSE_ESTIMATE`` and ``CONSERVATIVE_CHARGE`` remain in the enum
        for response-side accounting but are rejected here with
        ``RUN_BUDGET_FINAL_SETTLEMENT_REQUIRES_PROVIDER_BILL``; they must not
        release the worst-case reservation.

        The method locks the account and replaces the attempt's original hold
        with the explicit provider-billed amount.  A bill may be lower than the
        hold, equal to it, or larger than both the hold and the account limit.
        An over-limit bill is retained as settlement evidence: account
        availability becomes zero and the over-limit state remains observable.
        Every subsequent new reservation is rejected until the account is
        reconciled under a future policy.  This is post-event accounting and
        does not authorize pre-dispatch overspend.

        Malformed amounts or missing/surrounding-whitespace evidence raise
        ``TypeError`` or ``ValueError``.  Missing account/attempt rows, currency
        mismatch and non-bill evidence raise a documented
        ``RunBudgetLedgerError`` without partial account updates.  A settlement
        total that cannot fit ``NUMERIC(20, 8)`` raises
        ``RUN_BUDGET_AMOUNT_PRECISION_EXCEEDED`` and makes no ledger change; the
        trusted caller must retain the external provider bill outside this
        unwired boundary until a lossless billing-evidence store is integrated.

        Repeating identical provider-bill settlement content is idempotent; any
        different settlement for a settled attempt raises
        ``RUN_BUDGET_SETTLEMENT_CONFLICT``.  The caller owns commit or rollback.
        This method cannot verify external billing or exactly-once delivery.
        """

        _validate_budget_key(workspace_id, run_id)
        _validate_attempt_identity(attempt_identity)
        normalized_amount = _validate_amount(amount, field="amount", allow_zero=True)
        _validate_currency(currency)
        _validate_settlement_evidence(evidence)
        if evidence is not RunBudgetSettlementEvidence.PROVIDER_BILL:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_FINAL_SETTLEMENT_REQUIRES_PROVIDER_BILL",
                "Only a verified provider bill can finalize and release a reservation.",
            )
        _validate_evidence_identity(evidence_identity)
        is_estimate = False

        account = await self._lock_account(session, workspace_id, run_id)
        attempt = await self._lock_attempt(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            attempt_identity=attempt_identity,
        )
        if account.currency != currency or attempt.currency != currency:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_CURRENCY_MISMATCH",
                "The settlement currency differs from the attempt account currency.",
            )
        if attempt.status == RunBudgetAttemptStatus.SETTLED.value:
            _assert_matching_settlement(
                attempt,
                amount=normalized_amount,
                currency=currency,
                evidence=evidence,
                evidence_identity=evidence_identity,
            )
            return RunBudgetSettlement(
                account=_account_snapshot(account),
                attempt=_attempt_snapshot(attempt),
                created=False,
            )
        if attempt.status not in {
            RunBudgetAttemptStatus.RESERVED.value,
            RunBudgetAttemptStatus.UNKNOWN_OUTCOME.value,
        }:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ATTEMPT_STATE_CONFLICT",
                "The attempt has an unsupported budget state.",
            )
        if account.reserved_amount < attempt.reserved_amount:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_INVARIANT",
                "The account does not retain the attempt's original reservation.",
            )

        new_reserved = account.reserved_amount - attempt.reserved_amount
        new_settled = account.settled_amount + normalized_amount
        if new_reserved > MAX_AMOUNT or new_settled > MAX_AMOUNT:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_AMOUNT_PRECISION_EXCEEDED",
                "The settlement cannot fit NUMERIC(20, 8); no ledger change was posted.",
            )

        account.reserved_amount = new_reserved
        account.settled_amount = new_settled
        attempt.status = RunBudgetAttemptStatus.SETTLED.value
        attempt.settlement_amount = normalized_amount
        attempt.settlement_currency = currency
        attempt.settlement_evidence_kind = evidence.value
        attempt.settlement_evidence_identity = evidence_identity
        attempt.settlement_is_estimate = is_estimate
        attempt.settled_at = datetime.now(UTC)
        # Update the evidence first.  The account guard then verifies that the
        # aggregate still exactly explains its mutable totals.
        await session.flush([attempt])
        await session.flush([account])
        await session.refresh(account)
        await session.refresh(attempt)
        return RunBudgetSettlement(
            account=_account_snapshot(account),
            attempt=_attempt_snapshot(attempt),
            created=True,
        )

    async def get_account(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
    ) -> RunBudgetAccountSnapshot:
        """Read an account without locking it.

        A missing account raises ``RUN_BUDGET_ACCOUNT_NOT_FOUND``.  The returned
        amounts are a snapshot at the caller's current isolation level.
        """

        _validate_budget_key(workspace_id, run_id)
        account = await session.scalar(
            select(RunBudgetAccount)
            .where(
                RunBudgetAccount.workspace_id == workspace_id,
                RunBudgetAccount.run_id == run_id,
            )
            .execution_options(populate_existing=True)
        )
        if account is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_NOT_FOUND",
                "The Run budget account was not found.",
            )
        return _account_snapshot(account)

    async def get_attempt(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        attempt_identity: str,
    ) -> RunBudgetAttemptSnapshot:
        """Read one attempt without locking it.

        A missing attempt raises ``RUN_BUDGET_ATTEMPT_NOT_FOUND``.  The original
        reservation and its settlement evidence remain separately visible.
        """

        _validate_budget_key(workspace_id, run_id)
        _validate_attempt_identity(attempt_identity)
        attempt = await session.scalar(
            select(RunBudgetAttempt)
            .where(
                RunBudgetAttempt.workspace_id == workspace_id,
                RunBudgetAttempt.run_id == run_id,
                RunBudgetAttempt.attempt_identity == attempt_identity,
            )
            .execution_options(populate_existing=True)
        )
        if attempt is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ATTEMPT_NOT_FOUND",
                "The Run budget attempt was not found.",
            )
        return _attempt_snapshot(attempt)

    async def _lock_account(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        run_id: UUID,
    ) -> RunBudgetAccount:
        account = await session.scalar(
            select(RunBudgetAccount)
            .where(
                RunBudgetAccount.workspace_id == workspace_id,
                RunBudgetAccount.run_id == run_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if account is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_NOT_FOUND",
                "The Run budget account was not found.",
            )
        expected = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    RunBudgetAttempt.status.in_(
                                        [
                                            RunBudgetAttemptStatus.RESERVED.value,
                                            RunBudgetAttemptStatus.UNKNOWN_OUTCOME.value,
                                        ]
                                    ),
                                    RunBudgetAttempt.reserved_amount,
                                ),
                                else_=Decimal("0"),
                            )
                        ),
                        Decimal("0"),
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    RunBudgetAttempt.status == RunBudgetAttemptStatus.SETTLED.value,
                                    RunBudgetAttempt.settlement_amount,
                                ),
                                else_=Decimal("0"),
                            )
                        ),
                        Decimal("0"),
                    ),
                ).where(
                    RunBudgetAttempt.workspace_id == workspace_id,
                    RunBudgetAttempt.run_id == run_id,
                )
            )
        ).one()
        if (
            account.reserved_amount,
            account.settled_amount,
        ) != expected:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_INVARIANT",
                "The account totals differ from retained attempt evidence.",
            )
        return account

    async def _lock_attempt(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        attempt_identity: str,
    ) -> RunBudgetAttempt:
        attempt = await session.scalar(
            select(RunBudgetAttempt)
            .where(
                RunBudgetAttempt.workspace_id == workspace_id,
                RunBudgetAttempt.run_id == run_id,
                RunBudgetAttempt.attempt_identity == attempt_identity,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if attempt is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ATTEMPT_NOT_FOUND",
                "The Run budget attempt was not found.",
            )
        return attempt


def _validate_budget_key(workspace_id: UUID, run_id: UUID) -> None:
    _validate_uuid(workspace_id, "workspace_id")
    _validate_uuid(run_id, "run_id")


def _validate_uuid(value: Any, field: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field} must be a UUID")


def _validate_currency(value: Any) -> None:
    if not isinstance(value, str):
        raise TypeError("currency must be a string")
    if len(value) != 3 or any(not "A" <= character <= "Z" for character in value):
        raise ValueError("currency must be exactly three uppercase ASCII letters")


def _validate_amount(value: Any, *, field: str, allow_zero: bool) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value.is_signed() or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be {qualifier}")
    if value > MAX_AMOUNT:
        raise ValueError(f"{field} exceeds NUMERIC(20, 8)")

    _, digits, exponent = value.as_tuple()
    if exponent < -AMOUNT_SCALE:
        raise ValueError(f"{field} cannot have more than {AMOUNT_SCALE} decimal places")
    if value != 0:
        integer_digits = len(digits) + exponent if exponent >= 0 else max(len(digits) + exponent, 0)
        if integer_digits > AMOUNT_PRECISION - AMOUNT_SCALE:
            raise ValueError(f"{field} exceeds NUMERIC(20, 8)")
    return value


def _validate_attempt_identity(value: Any) -> None:
    _validate_bounded_text(
        value,
        field="attempt_identity",
        maximum=MAX_ATTEMPT_IDENTITY_LENGTH,
    )


def _validate_evidence_identity(value: Any) -> None:
    _validate_bounded_text(
        value,
        field="evidence_identity",
        maximum=MAX_EVIDENCE_IDENTITY_LENGTH,
    )


def _validate_bounded_text(value: Any, *, field: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty without surrounding whitespace")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    if "\x00" in value:
        raise ValueError(f"{field} cannot contain NUL")


def _validate_attempt_kind(value: Any) -> None:
    if not isinstance(value, RunBudgetAttemptKind):
        raise TypeError("attempt_kind must be a RunBudgetAttemptKind")


def _validate_reservation_basis(value: Any) -> None:
    if not isinstance(value, RunBudgetReservationBasis):
        raise TypeError("reservation_basis must be a RunBudgetReservationBasis")


def _validate_settlement_evidence(value: Any) -> None:
    if not isinstance(value, RunBudgetSettlementEvidence):
        raise TypeError("evidence must be a RunBudgetSettlementEvidence")


def _assert_matching_reservation(
    attempt: RunBudgetAttempt,
    *,
    amount: Decimal,
    currency: str,
    attempt_kind: RunBudgetAttemptKind,
    reservation_basis: RunBudgetReservationBasis,
) -> None:
    if (
        attempt.reserved_amount != amount
        or attempt.currency != currency
        or attempt.attempt_kind != attempt_kind.value
        or attempt.reservation_basis != reservation_basis.value
    ):
        raise RunBudgetLedgerError(
            "RUN_BUDGET_ATTEMPT_CONFLICT",
            "The attempt identity was reused with different reservation content.",
        )


def _assert_matching_settlement(
    attempt: RunBudgetAttempt,
    *,
    amount: Decimal,
    currency: str,
    evidence: RunBudgetSettlementEvidence,
    evidence_identity: str,
) -> None:
    if (
        attempt.settlement_amount != amount
        or attempt.settlement_currency != currency
        or attempt.settlement_evidence_kind != evidence.value
        or attempt.settlement_evidence_identity != evidence_identity
        or attempt.settlement_is_estimate
        != (evidence is not RunBudgetSettlementEvidence.PROVIDER_BILL)
    ):
        raise RunBudgetLedgerError(
            "RUN_BUDGET_SETTLEMENT_CONFLICT",
            "The attempt was already settled with different evidence or amount.",
        )


def _account_snapshot(account: RunBudgetAccount) -> RunBudgetAccountSnapshot:
    return RunBudgetAccountSnapshot(
        workspace_id=account.workspace_id,
        run_id=account.run_id,
        currency=account.currency,
        limit_amount=account.limit_amount,
        reserved_amount=account.reserved_amount,
        settled_amount=account.settled_amount,
    )


def _attempt_snapshot(attempt: RunBudgetAttempt) -> RunBudgetAttemptSnapshot:
    try:
        attempt_kind = RunBudgetAttemptKind(attempt.attempt_kind)
        reservation_basis = RunBudgetReservationBasis(attempt.reservation_basis)
        status = RunBudgetAttemptStatus(attempt.status)
        evidence = (
            RunBudgetSettlementEvidence(attempt.settlement_evidence_kind)
            if attempt.settlement_evidence_kind is not None
            else None
        )
    except ValueError as error:
        raise RunBudgetLedgerError(
            "RUN_BUDGET_ATTEMPT_INVARIANT",
            "The persisted attempt contains an unsupported budget value.",
        ) from error
    return RunBudgetAttemptSnapshot(
        id=attempt.id,
        workspace_id=attempt.workspace_id,
        run_id=attempt.run_id,
        attempt_identity=attempt.attempt_identity,
        attempt_kind=attempt_kind,
        reserved_amount=attempt.reserved_amount,
        currency=attempt.currency,
        reservation_basis=reservation_basis,
        status=status,
        settlement_amount=attempt.settlement_amount,
        settlement_currency=attempt.settlement_currency,
        settlement_evidence=evidence,
        settlement_evidence_identity=attempt.settlement_evidence_identity,
        settlement_is_estimate=attempt.settlement_is_estimate,
        settled_at=attempt.settled_at,
        created_at=attempt.created_at,
    )


__all__ = [
    "MAX_AMOUNT",
    "MAX_ATTEMPT_IDENTITY_LENGTH",
    "MAX_EVIDENCE_IDENTITY_LENGTH",
    "RunBudgetAccount",
    "RunBudgetAccountSnapshot",
    "RunBudgetAttempt",
    "RunBudgetAttemptKind",
    "RunBudgetAttemptSnapshot",
    "RunBudgetAttemptStatus",
    "RunBudgetLedger",
    "RunBudgetLedgerError",
    "RunBudgetReservation",
    "RunBudgetReservationBasis",
    "RunBudgetSettlement",
    "RunBudgetSettlementEvidence",
]
