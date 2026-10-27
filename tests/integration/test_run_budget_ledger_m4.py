"""PostgreSQL acceptance tests for the isolated M4 Run budget ledger."""

from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.agent_runtime.run_budget_ledger import (
    MAX_AMOUNT,
    RunBudgetAccount,
    RunBudgetAttempt,
    RunBudgetAttemptKind,
    RunBudgetAttemptStatus,
    RunBudgetLedger,
    RunBudgetLedgerError,
    RunBudgetReservationBasis,
    RunBudgetSettlementEvidence,
)
from packages.core.config.settings import get_settings
from packages.core.database import Base, create_database

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
EXPECTED_DATABASE_URL = (
    "postgresql+psycopg://agenthub:m2_local_test@127.0.0.1:15432/agenthub_run_budget_m4_20261028_v4"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set the dedicated Run budget test database URL to run these tests.",
    ),
]


@dataclass(frozen=True, slots=True)
class Tenant:
    user_id: UUID
    workspace_id: UUID
    run_id: UUID


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    assert TEST_DATABASE_URL == EXPECTED_DATABASE_URL, (
        "Run budget tests may only use agenthub_run_budget_m4_20261028_v4 "
        "on the assigned local PostgreSQL container."
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
    engine, factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest.fixture
def ledger() -> RunBudgetLedger:
    return RunBudgetLedger()


def _hex64() -> str:
    return uuid4().hex + uuid4().hex


async def _seed_tenant(
    factory: async_sessionmaker[AsyncSession],
    *,
    label: str | None = None,
) -> Tenant:
    label = label or "tenant"
    seed_suffix = uuid4().hex
    user_id = uuid4()
    organization_id = uuid4()
    workspace_id = uuid4()
    credential_id = uuid4()
    profile_id = uuid4()
    agent_id = uuid4()
    version_id = uuid4()
    run_id = uuid4()

    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO users (id, email, normalized_email, password_hash)
                    VALUES (:id, :email, :email, 'not-used')
                    """
                ),
                {
                    "id": user_id,
                    "email": f"run-budget-{label}-{seed_suffix}@example.test",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO organizations (id, name, created_by)
                    VALUES (:id, :name, :created_by)
                    """
                ),
                {
                    "id": organization_id,
                    "name": f"Run budget {label} {seed_suffix}",
                    "created_by": user_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO workspaces (id, organization_id, name)
                    VALUES (:id, :organization_id, :name)
                    """
                ),
                {
                    "id": workspace_id,
                    "organization_id": organization_id,
                    "name": f"Run budget {label} {seed_suffix}",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO provider_credentials (
                      id, workspace_id, provider, name, secret
                    )
                    VALUES (
                      :id, :workspace_id, 'offline', :name, 'offline-test-secret'
                    )
                    """
                ),
                {
                    "id": credential_id,
                    "workspace_id": workspace_id,
                    "name": f"run-budget-credential-{label}-{seed_suffix}",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO model_profiles (
                      id, workspace_id, provider_credential_id, model,
                      temperature, max_tokens, timeout_seconds, capabilities
                    )
                    VALUES (
                      :id, :workspace_id, :credential_id, 'offline-budget-model',
                      0, 512, 5, '{"streaming": false}'::jsonb
                    )
                    """
                ),
                {
                    "id": profile_id,
                    "workspace_id": workspace_id,
                    "credential_id": credential_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO agents (
                      id, workspace_id, name, system_prompt, prompt_version,
                      model_profile_id, knowledge_binding_mode,
                      model_retry_policy, retrieval_config, runtime_config
                    )
                    VALUES (
                      :id, :workspace_id, :name, 'Offline budget ledger test.',
                      1, :profile_id, 'PINNED', '{"max_attempts": 1}'::jsonb,
                      '{"knowledge_binding_mode": "PINNED", "knowledge_snapshots": []}'::jsonb,
                      '{}'::jsonb
                    )
                    """
                ),
                {
                    "id": agent_id,
                    "workspace_id": workspace_id,
                    "name": f"run-budget-agent-{label}-{seed_suffix}",
                    "profile_id": profile_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO agent_versions (
                      id, workspace_id, agent_id, version_number,
                      spec_schema_version, resolved_spec, resolved_spec_hash,
                      created_by
                    )
                    VALUES (
                      :id, :workspace_id, :agent_id, 1, 1,
                      '{"spec_schema_version": 1,
                        "model": {"model": "offline-budget-model"}}'::jsonb,
                      :resolved_spec_hash, :created_by
                    )
                    """
                ),
                {
                    "id": version_id,
                    "workspace_id": workspace_id,
                    "agent_id": agent_id,
                    "resolved_spec_hash": _hex64(),
                    "created_by": user_id,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                      id, workspace_id, agent_version_id, input_text,
                      status, created_by
                    )
                    VALUES (
                      :id, :workspace_id, :version_id, 'budget fixture',
                      'SUCCEEDED', :created_by
                    )
                    """
                ),
                {
                    "id": run_id,
                    "workspace_id": workspace_id,
                    "version_id": version_id,
                    "created_by": user_id,
                },
            )
    return Tenant(
        user_id=user_id,
        workspace_id=workspace_id,
        run_id=run_id,
    )


async def _create_account(
    factory: async_sessionmaker[AsyncSession],
    ledger: RunBudgetLedger,
    tenant: Tenant,
    *,
    limit_amount: Decimal = Decimal("100"),
) -> None:
    async with factory() as session:
        async with session.begin():
            await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="CNY",
                limit_amount=limit_amount,
            )


async def _reserve(
    factory: async_sessionmaker[AsyncSession],
    ledger: RunBudgetLedger,
    tenant: Tenant,
    *,
    attempt_identity: str,
    amount: Decimal,
    attempt_kind: RunBudgetAttemptKind = RunBudgetAttemptKind.MAIN_MODEL,
    reservation_basis: RunBudgetReservationBasis = (RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM),
):
    async with factory() as session:
        async with session.begin():
            return await ledger.reserve(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity=attempt_identity,
                attempt_kind=attempt_kind,
                amount=amount,
                currency="CNY",
                reservation_basis=reservation_basis,
            )


@pytest.mark.asyncio
async def test_migration_and_orm_define_the_retained_ledger_contract(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db_resources
    async with factory() as session:
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        table_columns = (
            await session.execute(
                text(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name IN ('run_budget_accounts', 'run_budget_attempts')
                    ORDER BY table_name, ordinal_position
                    """
                )
            )
        ).all()
        constraints = set(
            (
                await session.execute(
                    text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid IN (
                          'run_budget_accounts'::regclass,
                          'run_budget_attempts'::regclass
                        )
                        """
                    )
                )
            ).scalars()
        )
        triggers = set(
            (
                await session.execute(
                    text(
                        """
                        SELECT trigger_name
                        FROM information_schema.triggers
                        WHERE trigger_schema = 'public'
                          AND event_object_table IN (
                            'run_budget_accounts',
                            'run_budget_attempts'
                          )
                        """
                    )
                )
            ).scalars()
        )

    assert version == "0045_run_budget_ledger"
    actual_columns: dict[str, set[str]] = {
        "run_budget_accounts": set(),
        "run_budget_attempts": set(),
    }
    for table_name, column_name in table_columns:
        actual_columns[table_name].add(column_name)
    assert actual_columns["run_budget_accounts"] == set(RunBudgetAccount.__table__.columns.keys())
    assert actual_columns["run_budget_attempts"] == set(RunBudgetAttempt.__table__.columns.keys())
    assert {
        "pk_run_budget_accounts",
        "pk_run_budget_attempts",
        "uq_run_budget_attempts_identity",
        "fk_run_budget_accounts_run_workspace",
        "fk_run_budget_attempts_account_scope",
        "fk_run_budget_attempts_run_workspace",
        "ck_run_budget_attempts_reservation_basis",
        "ck_run_budget_attempts_settlement_shape",
        "ck_run_budget_attempts_settlement_currency_matches",
        "ck_run_budget_attempts_final_settlement_provider_bill",
    } <= constraints
    assert {
        "run_budget_account_identity_guard",
        "run_budget_attempt_evidence_guard",
        "run_budget_attempt_totals_guard",
    } <= triggers
    assert set(Base.metadata.tables) >= {
        "run_budget_accounts",
        "run_budget_attempts",
    }


@pytest.mark.asyncio
async def test_account_is_idempotent_and_rejects_malformed_limits(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)

    async with factory() as session:
        async with session.begin():
            account, created = await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="CNY",
                limit_amount=Decimal("100.00"),
            )
    assert created is True
    assert account.limit_amount == Decimal("100.00000000")
    assert account.available_amount == Decimal("100.00000000")

    async with factory() as session:
        async with session.begin():
            duplicate, created = await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="CNY",
                limit_amount=Decimal("100"),
            )
    assert created is False
    assert duplicate == account

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as raised:
            async with session.begin():
                await ledger.create_account(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    currency="CNY",
                    limit_amount=Decimal("101"),
                )
    assert raised.value.code == "RUN_BUDGET_ACCOUNT_CONFLICT"

    for invalid in (
        Decimal("0"),
        Decimal("-0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("0.000000001"),
        Decimal("1000000000000"),
    ):
        async with factory() as session:
            with pytest.raises(ValueError):
                await ledger.create_account(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    currency="CNY",
                    limit_amount=invalid,
                )
    async with factory() as session:
        with pytest.raises(TypeError):
            await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="CNY",
                limit_amount=100.0,
            )
        with pytest.raises(ValueError):
            await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="cny",
                limit_amount=Decimal("100"),
            )


@pytest.mark.asyncio
async def test_estimate_basis_cannot_create_attempt_or_charge_account(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    async with factory() as session:
        async with session.begin():
            created_account, _ = await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="CNY",
                limit_amount=Decimal("100"),
            )

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as rejected:
            async with session.begin():
                await ledger.reserve(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    attempt_identity="estimate:service",
                    attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                    amount=Decimal("10"),
                    currency="CNY",
                    reservation_basis=RunBudgetReservationBasis.ESTIMATE,
                )
    assert rejected.value.code == "RUN_BUDGET_CONSERVATIVE_MAXIMUM_REQUIRED"

    async with factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO run_budget_attempts (
                          id, workspace_id, run_id, attempt_identity,
                          attempt_kind, reserved_amount, currency,
                          reservation_basis, status
                        )
                        VALUES (
                          :id, :workspace_id, :run_id, 'estimate:direct-sql',
                          'MAIN_MODEL', 10, 'CNY',
                          'ESTIMATE', 'RESERVED'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "workspace_id": tenant.workspace_id,
                        "run_id": tenant.run_id,
                    },
                )

    async with factory() as session:
        unchanged = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        attempt_count = await session.scalar(
            text(
                """
                SELECT count(*)
                FROM run_budget_attempts
                WHERE workspace_id = :workspace_id AND run_id = :run_id
                """
            ),
            {
                "workspace_id": tenant.workspace_id,
                "run_id": tenant.run_id,
            },
        )
    assert unchanged == created_account
    assert unchanged.reserved_amount == Decimal("0.00000000")
    assert attempt_count == 0


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overallocate_one_account(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant)
    barrier = asyncio.Barrier(2)

    async def contender(identity: str):
        async with factory() as session:
            async with session.begin():
                await barrier.wait()
                return await ledger.reserve(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    attempt_identity=identity,
                    attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                    amount=Decimal("70"),
                    currency="CNY",
                    reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
                )

    outcomes = await asyncio.gather(
        contender("main:1"),
        contender("fallback:1"),
        return_exceptions=True,
    )
    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RunBudgetLedgerError)
    assert failures[0].code == "RUN_BUDGET_INSUFFICIENT_FUNDS"

    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        attempt_count = await session.scalar(
            text(
                """
                SELECT count(*)
                FROM run_budget_attempts
                WHERE workspace_id = :workspace_id AND run_id = :run_id
                """
            ),
            {
                "workspace_id": tenant.workspace_id,
                "run_id": tenant.run_id,
            },
        )
    assert account.reserved_amount == Decimal("70.00000000")
    assert account.available_amount == Decimal("30.00000000")
    assert attempt_count == 1


@pytest.mark.asyncio
async def test_reused_session_refreshes_identity_map_across_transactions(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant)
    await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="reuse:held",
        amount=Decimal("10"),
    )

    async with factory() as session_a:
        initial_account = await ledger.get_account(
            session_a,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        initial_attempt = await ledger.get_attempt(
            session_a,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="reuse:held",
        )
        await session_a.commit()
    assert initial_account.reserved_amount == Decimal("10.00000000")
    assert initial_attempt.status is RunBudgetAttemptStatus.RESERVED

    async with factory() as session_b:
        async with session_b.begin():
            await ledger.reserve(
                session_b,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="reuse:new",
                attempt_kind=RunBudgetAttemptKind.FALLBACK,
                amount=Decimal("80"),
                currency="CNY",
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )
            await ledger.mark_unknown_outcome(
                session_b,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="reuse:held",
            )

    async with session_a.begin():
        duplicate = await ledger.reserve(
            session_a,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="reuse:held",
            attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
            amount=Decimal("10"),
            currency="CNY",
            reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
        )
    assert duplicate.created is False
    assert duplicate.account.reserved_amount == Decimal("90.00000000")
    assert duplicate.account.available_amount == Decimal("10.00000000")
    assert duplicate.attempt.status is RunBudgetAttemptStatus.UNKNOWN_OUTCOME

    async with session_a.begin():
        new_attempt = await ledger.get_attempt(
            session_a,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="reuse:new",
        )
    assert new_attempt.status is RunBudgetAttemptStatus.RESERVED

    async with factory() as session_b:
        async with session_b.begin():
            await ledger.settle(
                session_b,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="reuse:held",
                amount=Decimal("5"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="provider-bill:reuse-held",
            )

    async with session_a.begin():
        refreshed_account = await ledger.get_account(
            session_a,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        refreshed_attempt = await ledger.get_attempt(
            session_a,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="reuse:held",
        )
    assert refreshed_account.reserved_amount == Decimal("80.00000000")
    assert refreshed_account.settled_amount == Decimal("5.00000000")
    assert refreshed_account.available_amount == Decimal("15.00000000")
    assert refreshed_attempt.status is RunBudgetAttemptStatus.SETTLED
    assert refreshed_attempt.settlement_amount == Decimal("5.00000000")

    with pytest.raises(RunBudgetLedgerError) as blocked:
        async with session_a.begin():
            await ledger.reserve(
                session_a,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="reuse:blocked",
                attempt_kind=RunBudgetAttemptKind.AUXILIARY,
                amount=Decimal("20"),
                currency="CNY",
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )
    assert blocked.value.code == "RUN_BUDGET_INSUFFICIENT_FUNDS"


@pytest.mark.asyncio
async def test_attempt_identity_is_idempotent_only_for_identical_content(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant)

    first = await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="run:1:main:1",
        amount=Decimal("20"),
        reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
    )
    duplicate = await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="run:1:main:1",
        amount=Decimal("20.00000000"),
        reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
    )
    assert first.created is True
    assert duplicate.created is False
    assert duplicate.attempt.id == first.attempt.id
    assert duplicate.account.reserved_amount == Decimal("20.00000000")

    conflicts = (
        {
            "amount": Decimal("21"),
            "attempt_kind": RunBudgetAttemptKind.MAIN_MODEL,
            "reservation_basis": RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
        },
        {
            "amount": Decimal("20"),
            "attempt_kind": RunBudgetAttemptKind.RETRY,
            "reservation_basis": RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
        },
    )
    for conflict in conflicts:
        async with factory() as session:
            with pytest.raises(RunBudgetLedgerError) as raised:
                async with session.begin():
                    await ledger.reserve(
                        session,
                        workspace_id=tenant.workspace_id,
                        run_id=tenant.run_id,
                        attempt_identity="run:1:main:1",
                        currency="CNY",
                        **conflict,
                    )
        assert raised.value.code == "RUN_BUDGET_ATTEMPT_CONFLICT"

    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        stored = await ledger.get_attempt(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="run:1:main:1",
        )
    assert account.reserved_amount == Decimal("20.00000000")
    assert stored.reserved_amount == Decimal("20.00000000")
    assert stored.attempt_kind is RunBudgetAttemptKind.MAIN_MODEL
    assert stored.reservation_basis is RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM


@pytest.mark.asyncio
async def test_cross_tenant_run_and_account_lookups_are_rejected(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    first = await _seed_tenant(factory, label="tenant-a")
    second = await _seed_tenant(factory, label="tenant-b")
    await _create_account(factory, ledger, first)
    await _create_account(factory, ledger, second)

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as missing:
            async with session.begin():
                await ledger.reserve(
                    session,
                    workspace_id=first.workspace_id,
                    run_id=second.run_id,
                    attempt_identity="tenant-escape",
                    attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                    amount=Decimal("1"),
                    currency="CNY",
                    reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
                )
    assert missing.value.code == "RUN_BUDGET_ACCOUNT_NOT_FOUND"

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as wrong_run:
            async with session.begin():
                await ledger.create_account(
                    session,
                    workspace_id=second.workspace_id,
                    run_id=first.run_id,
                    currency="CNY",
                    limit_amount=Decimal("10"),
                )
        escaped = await session.scalar(
            text(
                """
                SELECT count(*)
                FROM run_budget_attempts
                WHERE attempt_identity = 'tenant-escape'
                """
            )
        )
    assert wrong_run.value.code == "RUN_BUDGET_RUN_NOT_FOUND"
    assert escaped == 0


@pytest.mark.asyncio
async def test_unknown_outcome_retains_the_full_hold_until_trusted_settlement(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant)
    await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="summary:round-1",
        amount=Decimal("30"),
        attempt_kind=RunBudgetAttemptKind.SUMMARY,
        reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
    )

    # Closing without a settlement models a worker crash after reserve commit.
    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        attempt = await ledger.get_attempt(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="summary:round-1",
        )
    assert account.reserved_amount == Decimal("30.00000000")
    assert attempt.status is RunBudgetAttemptStatus.RESERVED

    async with factory() as session:
        async with session.begin():
            unknown = await ledger.mark_unknown_outcome(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="summary:round-1",
            )
    assert unknown.status is RunBudgetAttemptStatus.UNKNOWN_OUTCOME
    assert unknown.reserved_amount == Decimal("30.00000000")

    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
    assert account.reserved_amount == Decimal("30.00000000")
    assert account.settled_amount == Decimal("0.00000000")

    async with factory() as session:
        async with session.begin():
            settlement = await ledger.settle(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="summary:round-1",
                amount=Decimal("30"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="provider-bill:summary-round-1",
            )
    assert settlement.created is True
    assert settlement.account.reserved_amount == Decimal("0.00000000")
    assert settlement.account.settled_amount == Decimal("30.00000000")
    assert settlement.attempt.reserved_amount == Decimal("30.00000000")
    assert settlement.attempt.settlement_amount == Decimal("30.00000000")
    assert settlement.attempt.settlement_is_estimate is False
    assert settlement.attempt.status is RunBudgetAttemptStatus.SETTLED

    async with factory() as session:
        async with session.begin():
            duplicate = await ledger.settle(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="summary:round-1",
                amount=Decimal("30.00000000"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="provider-bill:summary-round-1",
            )
    assert duplicate.created is False
    assert duplicate.account == settlement.account

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as conflict:
            async with session.begin():
                await ledger.settle(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    attempt_identity="summary:round-1",
                    amount=Decimal("29"),
                    currency="CNY",
                    evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                    evidence_identity="provider-bill:summary-round-1",
                )
    assert conflict.value.code == "RUN_BUDGET_SETTLEMENT_CONFLICT"


@pytest.mark.asyncio
async def test_only_provider_bill_finalizes_and_over_limit_bill_is_retained(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant, limit_amount=Decimal("100"))
    await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="model:bill",
        amount=Decimal("99"),
        reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
    )

    for evidence in (
        RunBudgetSettlementEvidence.RESPONSE_ESTIMATE,
        RunBudgetSettlementEvidence.CONSERVATIVE_CHARGE,
    ):
        async with factory() as session:
            with pytest.raises(RunBudgetLedgerError) as rejected:
                async with session.begin():
                    await ledger.settle(
                        session,
                        workspace_id=tenant.workspace_id,
                        run_id=tenant.run_id,
                        attempt_identity="model:bill",
                        amount=Decimal("10"),
                        currency="CNY",
                        evidence=evidence,
                        evidence_identity=f"non-final:{evidence.value}",
                    )
        assert rejected.value.code == "RUN_BUDGET_FINAL_SETTLEMENT_REQUIRES_PROVIDER_BILL"

    async with factory() as session:
        held = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        reserved = await ledger.get_attempt(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="model:bill",
        )
    assert held.reserved_amount == Decimal("99.00000000")
    assert held.settled_amount == Decimal("0.00000000")
    assert held.available_amount == Decimal("1.00000000")
    assert reserved.status is RunBudgetAttemptStatus.RESERVED
    assert reserved.settlement_amount is None

    async with factory() as session:
        async with session.begin():
            settled = await ledger.settle(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="model:bill",
                amount=Decimal("101"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="provider-bill:model-bill",
            )
    assert settled.account.reserved_amount == Decimal("0.00000000")
    assert settled.account.settled_amount == Decimal("101.00000000")
    assert settled.account.committed_amount == Decimal("101.00000000")
    assert settled.account.available_amount == Decimal("0.00000000")
    assert settled.account.overage_amount == Decimal("1.00000000")
    assert settled.account.is_over_limit is True
    assert settled.attempt.reserved_amount == Decimal("99.00000000")
    assert settled.attempt.settlement_amount == Decimal("101.00000000")
    assert settled.attempt.settlement_is_estimate is False
    assert settled.attempt.status is RunBudgetAttemptStatus.SETTLED

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as blocked:
            async with session.begin():
                await ledger.reserve(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    attempt_identity="model:after-overrun",
                    attempt_kind=RunBudgetAttemptKind.FALLBACK,
                    amount=Decimal("1"),
                    currency="CNY",
                    reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
                )
    assert blocked.value.code == "RUN_BUDGET_INSUFFICIENT_FUNDS"

    # A direct database writer must not defeat the same rule by inserting an
    # attempt and updating the aggregate consistently in one transaction.
    async with factory() as session:
        with pytest.raises(DBAPIError, match="cannot add an over-limit reservation"):
            async with session.begin():
                session.add(
                    RunBudgetAttempt(
                        id=uuid4(),
                        workspace_id=tenant.workspace_id,
                        run_id=tenant.run_id,
                        attempt_identity="model:direct-overrun",
                        attempt_kind=RunBudgetAttemptKind.MAIN_MODEL.value,
                        reserved_amount=Decimal("1"),
                        currency="CNY",
                        reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM.value,
                        status=RunBudgetAttemptStatus.RESERVED.value,
                    )
                )
                await session.flush()
                await session.execute(
                    text(
                        "UPDATE run_budget_accounts SET reserved_amount = reserved_amount + 1 "
                        "WHERE workspace_id = :workspace_id AND run_id = :run_id"
                    ),
                    {"workspace_id": tenant.workspace_id, "run_id": tenant.run_id},
                )

    async with factory() as session:
        final = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
    assert final == settled.account

    lower_bill_tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, lower_bill_tenant)
    await _reserve(
        factory,
        ledger,
        lower_bill_tenant,
        attempt_identity="model:lower-bill",
        amount=Decimal("40"),
    )
    async with factory() as session:
        async with session.begin():
            lower_bill = await ledger.settle(
                session,
                workspace_id=lower_bill_tenant.workspace_id,
                run_id=lower_bill_tenant.run_id,
                attempt_identity="model:lower-bill",
                amount=Decimal("10"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="provider-bill:lower-bill",
            )
    assert lower_bill.account.reserved_amount == Decimal("0.00000000")
    assert lower_bill.account.settled_amount == Decimal("10.00000000")
    assert lower_bill.account.available_amount == Decimal("90.00000000")
    assert lower_bill.account.is_over_limit is False
    assert lower_bill.attempt.reserved_amount == Decimal("40.00000000")
    assert lower_bill.attempt.settlement_amount == Decimal("10.00000000")


@pytest.mark.asyncio
async def test_precision_overflow_does_not_post_a_partial_settlement(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant, limit_amount=MAX_AMOUNT)
    await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="precision:first",
        amount=MAX_AMOUNT - Decimal("1"),
    )
    await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="precision:second",
        amount=Decimal("1"),
    )

    async with factory() as session:
        async with session.begin():
            settled = await ledger.settle(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="precision:first",
                amount=MAX_AMOUNT,
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="provider-bill:precision-first",
            )
    assert settled.account.settled_amount == MAX_AMOUNT
    assert settled.account.reserved_amount == Decimal("1.00000000")
    assert settled.account.available_amount == Decimal("0.00000000")

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as overflow:
            async with session.begin():
                await ledger.settle(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    attempt_identity="precision:second",
                    amount=Decimal("1"),
                    currency="CNY",
                    evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                    evidence_identity="provider-bill:precision-second",
                )
    assert overflow.value.code == "RUN_BUDGET_AMOUNT_PRECISION_EXCEEDED"

    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        unposted = await ledger.get_attempt(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="precision:second",
        )
    assert account == settled.account
    assert unposted.status is RunBudgetAttemptStatus.RESERVED
    assert unposted.settlement_amount is None


@pytest.mark.asyncio
async def test_settlement_requires_evidence_and_matching_currency(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant)
    await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="model:evidence",
        amount=Decimal("15"),
    )

    async with factory() as session:
        with pytest.raises(ValueError):
            await ledger.settle(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="model:evidence",
                amount=Decimal("15"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity=" ",
            )

    async with factory() as session:
        with pytest.raises(RunBudgetLedgerError) as mismatch:
            async with session.begin():
                await ledger.settle(
                    session,
                    workspace_id=tenant.workspace_id,
                    run_id=tenant.run_id,
                    attempt_identity="model:evidence",
                    amount=Decimal("15"),
                    currency="USD",
                    evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                    evidence_identity="provider-bill:evidence",
                )
    assert mismatch.value.code == "RUN_BUDGET_CURRENCY_MISMATCH"

    async with factory() as session:
        with pytest.raises(DBAPIError, match="currency must match"):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO run_budget_attempts (
                          id, workspace_id, run_id, attempt_identity,
                          attempt_kind, reserved_amount, currency,
                          reservation_basis, status
                        )
                        VALUES (
                          :id, :workspace_id, :run_id, 'direct:currency-mismatch',
                          'MAIN_MODEL', 1, 'USD',
                          'CONSERVATIVE_MAXIMUM', 'RESERVED'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "workspace_id": tenant.workspace_id,
                        "run_id": tenant.run_id,
                    },
                )

    async with factory() as session:
        with pytest.raises(DBAPIError, match="settlement currency must match"):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE run_budget_attempts
                        SET status = 'SETTLED',
                            settlement_amount = 15,
                            settlement_currency = 'USD',
                            settlement_evidence_kind = 'PROVIDER_BILL',
                            settlement_evidence_identity = 'direct:wrong-currency',
                            settlement_is_estimate = false,
                            settled_at = now()
                        WHERE workspace_id = :workspace_id
                          AND run_id = :run_id
                          AND attempt_identity = 'model:evidence'
                        """
                    ),
                    {
                        "workspace_id": tenant.workspace_id,
                        "run_id": tenant.run_id,
                    },
                )

    async with factory() as session:
        account = await ledger.get_account(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
        )
        attempt = await ledger.get_attempt(
            session,
            workspace_id=tenant.workspace_id,
            run_id=tenant.run_id,
            attempt_identity="model:evidence",
        )
    assert account.reserved_amount == Decimal("15.00000000")
    assert attempt.status is RunBudgetAttemptStatus.RESERVED


@pytest.mark.asyncio
async def test_transaction_rollback_removes_new_accounts_and_reservations(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)

    async with factory() as session:
        transaction = await session.begin()
        try:
            await ledger.create_account(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                currency="CNY",
                limit_amount=Decimal("100"),
            )
            await ledger.reserve(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="rollback:account",
                attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                amount=Decimal("20"),
                currency="CNY",
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )
        finally:
            await transaction.rollback()

    async with factory() as session:
        account_count = await session.scalar(
            text(
                """
                SELECT count(*)
                FROM run_budget_accounts
                WHERE workspace_id = :workspace_id AND run_id = :run_id
                """
            ),
            {
                "workspace_id": tenant.workspace_id,
                "run_id": tenant.run_id,
            },
        )
    assert account_count == 0

    await _create_account(factory, ledger, tenant)
    async with factory() as session:
        transaction = await session.begin()
        try:
            await ledger.reserve(
                session,
                workspace_id=tenant.workspace_id,
                run_id=tenant.run_id,
                attempt_identity="rollback:reservation",
                attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                amount=Decimal("20"),
                currency="CNY",
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )
        finally:
            await transaction.rollback()

    async with factory() as session:
        account = await session.scalar(
            select(RunBudgetAccount).where(
                RunBudgetAccount.workspace_id == tenant.workspace_id,
                RunBudgetAccount.run_id == tenant.run_id,
            )
        )
        attempts = (
            (
                await session.execute(
                    select(RunBudgetAttempt).where(
                        RunBudgetAttempt.workspace_id == tenant.workspace_id,
                        RunBudgetAttempt.run_id == tenant.run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert account is not None
    assert account.reserved_amount == Decimal("0.00000000")
    assert account.settled_amount == Decimal("0.00000000")
    assert attempts == []


@pytest.mark.asyncio
async def test_retained_rows_resist_rewrite_and_destructive_migration(
    db_resources: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ledger: RunBudgetLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = db_resources
    tenant = await _seed_tenant(factory)
    await _create_account(factory, ledger, tenant)
    reservation = await _reserve(
        factory,
        ledger,
        tenant,
        attempt_identity="retained:1",
        amount=Decimal("10"),
    )

    async with factory() as session:
        with pytest.raises(DBAPIError, match="account totals do not match"):
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE run_budget_attempts
                        SET status = 'SETTLED',
                            settlement_amount = 10,
                            settlement_currency = 'CNY',
                            settlement_evidence_kind = 'PROVIDER_BILL',
                            settlement_evidence_identity = 'uncoordinated-direct-write',
                            settlement_is_estimate = false,
                            settled_at = now()
                        WHERE id = :id
                        """
                    ),
                    {"id": reservation.attempt.id},
                )

    async with factory() as session:
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        """
                        UPDATE run_budget_accounts
                        SET reserved_amount = 0
                        WHERE workspace_id = :workspace_id AND run_id = :run_id
                        """
                    ),
                    {
                        "workspace_id": tenant.workspace_id,
                        "run_id": tenant.run_id,
                    },
                )
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        """
                        UPDATE run_budget_accounts
                        SET limit_amount = 101
                        WHERE workspace_id = :workspace_id AND run_id = :run_id
                        """
                    ),
                    {
                        "workspace_id": tenant.workspace_id,
                        "run_id": tenant.run_id,
                    },
                )
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text("DELETE FROM run_budget_attempts WHERE id = :id"),
                    {"id": reservation.attempt.id},
                )

    migration = importlib.import_module("migrations.versions.0045_run_budget_ledger")
    sync_engine = create_engine(TEST_DATABASE_URL)
    try:
        with sync_engine.connect() as connection:
            monkeypatch.setattr(
                migration,
                "op",
                Operations(MigrationContext.configure(connection)),
            )
            with pytest.raises(
                DBAPIError,
                match="Cannot remove retained Run budget ledger evidence",
            ):
                with connection.begin_nested():
                    migration.downgrade()
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
            account_count = connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM run_budget_accounts
                    WHERE workspace_id = :workspace_id AND run_id = :run_id
                    """
                ),
                {
                    "workspace_id": tenant.workspace_id,
                    "run_id": tenant.run_id,
                },
            )
    finally:
        sync_engine.dispose()
    assert version == "0045_run_budget_ledger"
    assert account_count == 1
