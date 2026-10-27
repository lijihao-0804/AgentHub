"""PostgreSQL integration tests for the summary-to-Run budget transaction bridge."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAccount,
    RunBudgetAttempt,
    RunBudgetAttemptKind,
    RunBudgetAttemptStatus,
    RunBudgetLedger,
    RunBudgetReservationBasis,
)
from packages.agent_runtime.summary_budget_adapter import SummaryRunBudgetReserver
from packages.agent_runtime.summary_models import ThreadSummaryDispatch, ThreadSummaryJob
from packages.agent_runtime.summary_service import (
    SummaryJobBinding,
    SummaryJobKey,
    SummaryJobService,
    SummaryServiceError,
    SummaryVerifierPhase,
    _binding,
)
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from tests.integration.test_summary_service_m4 import (
    _new_summary_job,
    _seed_tenant,
    _Tenant,
    _validation_context,
    _Verifier,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://agenthub:m2_local_test@127.0.0.1:15432/agenthub_run_budget_m4_20261028_v4"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set the dedicated summary budget bridge database URL.",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    assert TEST_DATABASE_URL == EXPECTED_DATABASE_URL, (
        "Summary bridge tests may only use agenthub_run_budget_m4_20261028_v4."
    )
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_resources(
    migrated_database: None,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    del migrated_database
    database_url = TEST_DATABASE_URL.replace(
        "postgresql+psycopg://",
        "postgresql+asyncpg://",
        1,
    )
    engine, factory = create_database(database_url)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


def _unique_label(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


async def _create_account(
    factory: async_sessionmaker[AsyncSession],
    ledger: RunBudgetLedger,
    tenant: _Tenant,
    *,
    amount: Decimal = Decimal("1"),
    currency: str = "CNY",
) -> None:
    async with factory() as session:
        async with session.begin():
            await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency=currency,
                limit_amount=amount,
            )


def _service(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    budget_reserver,
) -> SummaryJobService:
    return SummaryJobService(
        factory,
        verifier=_Verifier(lambda binding: _validation_context(tenant)),
        budget_reserver=budget_reserver,
    )


async def _create_claimed(
    factory: async_sessionmaker[AsyncSession],
    tenant: _Tenant,
    budget_reserver,
) -> tuple[SummaryJobService, SummaryJobKey, str]:
    service = _service(factory, tenant, budget_reserver)
    created = await service.create_job(_new_summary_job(tenant))
    claimed = await service.claim(created.key)
    assert claimed is not None
    return service, created.key, claimed.lease_token


async def _commit_intent(
    service: SummaryJobService,
    key: SummaryJobKey,
    lease_token: str,
    tenant: _Tenant,
):
    return await service.commit_dispatch_intent(
        key,
        lease_token=lease_token,
        pricing_snapshot_id=tenant.pricing_id,
        reserved_input_tokens=700,
        reserved_output_tokens=300,
    )


async def _rejection_state(
    factory: async_sessionmaker[AsyncSession],
    key: SummaryJobKey,
    tenant: _Tenant,
) -> tuple[str, bool, Decimal | None, int, int]:
    async with factory() as session:
        job = await session.get(ThreadSummaryJob, key.job_id)
        assert job is not None
        account = await session.scalar(
            select(RunBudgetAccount).where(
                RunBudgetAccount.workspace_id == tenant.workspace_id,
                RunBudgetAccount.run_id == tenant.run_id,
            )
        )
        attempt_count = await session.scalar(
            select(func.count())
            .select_from(RunBudgetAttempt)
            .where(
                RunBudgetAttempt.workspace_id == tenant.workspace_id,
                RunBudgetAttempt.run_id == tenant.run_id,
            )
        )
        dispatch_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
        return (
            job.status,
            job.budget_reserved,
            None if account is None else account.reserved_amount,
            int(attempt_count or 0),
            int(dispatch_count or 0),
        )


class _FailAfterReservation:
    def __init__(self, adapter: SummaryRunBudgetReserver) -> None:
        self._adapter = adapter

    async def reserve(
        self,
        session: AsyncSession,
        *,
        binding: SummaryJobBinding,
        reservation_id: UUID,
        amount: Decimal,
        currency: str,
    ) -> bool:
        accepted = await self._adapter.reserve(
            session,
            binding=binding,
            reservation_id=reservation_id,
            amount=amount,
            currency=currency,
        )
        if accepted:
            raise RuntimeError("injected failure after ledger reservation")
        return False


@pytest.mark.asyncio
async def test_dispatch_intent_and_run_reservation_commit_together(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory, label=_unique_label("bridge-success"))
    ledger = RunBudgetLedger()
    adapter = SummaryRunBudgetReserver(ledger)
    await _create_account(factory, ledger, tenant)
    service, key, lease_token = await _create_claimed(factory, tenant, adapter)

    intent = await _commit_intent(service, key, lease_token, tenant)

    async with factory() as session:
        job = await session.get(ThreadSummaryJob, key.job_id)
        dispatch = await session.scalar(
            select(ThreadSummaryDispatch).where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
        attempt = await session.scalar(
            select(RunBudgetAttempt).where(
                RunBudgetAttempt.workspace_id == tenant.workspace_id,
                RunBudgetAttempt.run_id == tenant.run_id,
                RunBudgetAttempt.attempt_identity == f"summary:{intent.reservation_id}",
            )
        )
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
    assert job is not None and dispatch is not None and attempt is not None
    assert job.status == "DISPATCH_INTENT"
    assert job.budget_reserved is True
    assert job.dispatch_id == dispatch.id
    assert dispatch.reservation_id == intent.reservation_id
    assert dispatch.reserved_cost_amount == intent.reserved_cost_amount
    assert attempt.attempt_kind == RunBudgetAttemptKind.SUMMARY.value
    assert attempt.reservation_basis == RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM.value
    assert attempt.status == RunBudgetAttemptStatus.RESERVED.value
    assert attempt.reserved_amount == intent.reserved_cost_amount
    assert attempt.currency == intent.reserved_currency
    assert account.reserved_amount == intent.reserved_cost_amount

    binding = _binding(job, SummaryVerifierPhase.DISPATCH)
    async with factory() as session:
        async with session.begin():
            duplicate = await adapter.reserve(
                session,
                binding=binding,
                reservation_id=intent.reservation_id,
                amount=intent.reserved_cost_amount,
                currency=intent.reserved_currency,
            )
    assert duplicate is False


@pytest.mark.asyncio
async def test_rejection_and_outer_rollback_leave_no_account_charge_or_dispatch(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    ledger = RunBudgetLedger()
    adapter = SummaryRunBudgetReserver(ledger)

    missing_tenant = await _seed_tenant(factory, label=_unique_label("bridge-missing"))
    missing_service, missing_key, missing_lease = await _create_claimed(
        factory,
        missing_tenant,
        adapter,
    )
    with pytest.raises(SummaryServiceError) as missing:
        await _commit_intent(
            missing_service,
            missing_key,
            missing_lease,
            missing_tenant,
        )
    assert missing.value.code == "SUMMARY_SHARED_BUDGET_UNAVAILABLE"
    state = await _rejection_state(factory, missing_key, missing_tenant)
    assert state == ("CLAIMED", False, None, 0, 0)

    currency_tenant = await _seed_tenant(factory, label=_unique_label("bridge-currency"))
    await _create_account(factory, ledger, currency_tenant, currency="USD")
    currency_service, currency_key, currency_lease = await _create_claimed(
        factory,
        currency_tenant,
        adapter,
    )
    with pytest.raises(SummaryServiceError) as currency_mismatch:
        await _commit_intent(
            currency_service,
            currency_key,
            currency_lease,
            currency_tenant,
        )
    assert currency_mismatch.value.code == "SUMMARY_SHARED_BUDGET_UNAVAILABLE"
    state = await _rejection_state(factory, currency_key, currency_tenant)
    assert state == ("CLAIMED", False, Decimal("0.00000000"), 0, 0)

    insufficient_tenant = await _seed_tenant(
        factory,
        label=_unique_label("bridge-insufficient"),
    )
    await _create_account(
        factory,
        ledger,
        insufficient_tenant,
        amount=Decimal("0.00000001"),
    )
    insufficient_service, insufficient_key, insufficient_lease = await _create_claimed(
        factory,
        insufficient_tenant,
        adapter,
    )
    with pytest.raises(SummaryServiceError) as insufficient:
        await _commit_intent(
            insufficient_service,
            insufficient_key,
            insufficient_lease,
            insufficient_tenant,
        )
    assert insufficient.value.code == "SUMMARY_SHARED_BUDGET_UNAVAILABLE"
    state = await _rejection_state(factory, insufficient_key, insufficient_tenant)
    assert state == ("CLAIMED", False, Decimal("0.00000000"), 0, 0)

    rollback_tenant = await _seed_tenant(factory, label=_unique_label("bridge-rollback"))
    await _create_account(factory, ledger, rollback_tenant)
    rollback_service, rollback_key, rollback_lease = await _create_claimed(
        factory,
        rollback_tenant,
        _FailAfterReservation(adapter),
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        await _commit_intent(
            rollback_service,
            rollback_key,
            rollback_lease,
            rollback_tenant,
        )
    state = await _rejection_state(factory, rollback_key, rollback_tenant)
    assert state == ("CLAIMED", False, Decimal("0.00000000"), 0, 0)


@pytest.mark.asyncio
async def test_unknown_summary_outcome_keeps_budget_and_blocks_redispatch(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory, label=_unique_label("bridge-unknown"))
    ledger = RunBudgetLedger()
    adapter = SummaryRunBudgetReserver(ledger)
    await _create_account(factory, ledger, tenant)
    service, key, lease_token = await _create_claimed(factory, tenant, adapter)
    intent = await _commit_intent(service, key, lease_token, tenant)

    outcome = await service.mark_dispatch_unknown(key, lease_token=lease_token)
    assert outcome.status == "UNKNOWN_OUTCOME"

    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        attempt = await session.scalar(
            select(RunBudgetAttempt).where(
                RunBudgetAttempt.workspace_id == tenant.workspace_id,
                RunBudgetAttempt.run_id == tenant.run_id,
                RunBudgetAttempt.attempt_identity == f"summary:{intent.reservation_id}",
            )
        )
        dispatch_count = await session.scalar(
            select(func.count())
            .select_from(ThreadSummaryDispatch)
            .where(
                ThreadSummaryDispatch.workspace_id == key.workspace_id,
                ThreadSummaryDispatch.thread_id == key.thread_id,
                ThreadSummaryDispatch.job_id == key.job_id,
            )
        )
    assert account.reserved_amount == intent.reserved_cost_amount
    assert account.settled_amount == Decimal("0.00000000")
    assert attempt is not None
    assert attempt.status == RunBudgetAttemptStatus.RESERVED.value
    assert dispatch_count == 1
    assert await service.claim(key) is None
    with pytest.raises(SummaryServiceError) as redispatch:
        await _commit_intent(service, key, lease_token, tenant)
    assert redispatch.value.code == "SUMMARY_DISPATCH_STATE_INVALID"
