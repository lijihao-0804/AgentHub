"""PostgreSQL acceptance tests for the Interactive parent case budget."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.run_budget_ledger import (
    RunBudgetAccount,
    RunBudgetAttempt,
    RunBudgetAttemptKind,
    RunBudgetAttemptStatus,
    RunBudgetLedger,
    RunBudgetLedgerError,
    RunBudgetReservationBasis,
    RunBudgetSettlementEvidence,
)
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.runtime_projection import AgentRunExecutionOverrides
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.evaluation.case_budget_ledger import CaseBudgetLedger
from packages.evaluation.case_budget_models import (
    CaseBudgetAccount,
    CaseBudgetResponseObservation,
)
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.models import InteractiveEpisode
from packages.evaluation.service import EvaluationDatasetService
from packages.threads.service import ThreadService
from tests.integration.test_m7b_experiments import _manager_context
from tests.support.ae6b_database_urls import (
    async_test_database_url,
    require_isolated_test_database_url,
)
from tests.support.ae6b_lifecycle import (
    BoundEpisodeTurn,
    OwnedLifecycleCase,
    create_owned_lifecycle_cases,
    reserve_episode,
)

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to the dedicated case budget database.",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    require_isolated_test_database_url(TEST_DATABASE_URL)
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = async_test_database_url(
        TEST_DATABASE_URL
    ).render_as_string(hide_password=False)
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
    engine, factory = create_database(
        async_test_database_url(TEST_DATABASE_URL).render_as_string(hide_password=False)
    )
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class BudgetCaseFixture:
    owned: OwnedLifecycleCase
    episode: InteractiveEpisode
    turns: tuple[BoundEpisodeTurn, ...]


async def _cny_pricing(
    session: AsyncSession,
    base: dict[str, object],
    *,
    model: str = "frozen-model-a",
):
    provider = str(base.get("pricing_provider", "fake"))
    return await EvaluationDatasetService().create_pricing_snapshot(
        session,
        context=_manager_context(base),
        name="Synthetic CNY interactive pricing",
        provider=provider,
        model=model,
        currency="CNY",
        input_price_per_1m=Decimal("0.10"),
        output_price_per_1m=Decimal("0.20"),
        cached_input_price_per_1m=None,
        effective_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
        source_note="Synthetic fixture; trusted bound asserted by test caller.",
    )


async def _create_budget_case(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: Decimal = Decimal("10"),
    turn_count: int = 2,
    provider_egress_policy_version: str = "provider_egress_test_v1",
    provider_base_url: str | None = None,
) -> BudgetCaseFixture:
    import tests.support.ae6b_lifecycle as lifecycle_support

    monkeypatch.setattr(lifecycle_support, "_pricing", _cny_pricing)
    owned = (
        await create_owned_lifecycle_cases(
            factory,
            workspace_count=1,
            repetitions=1,
        )
    )[0]
    lifecycle = InteractiveLifecycle(factory)
    episode = await reserve_episode(lifecycle, owned)
    await lifecycle.create_fixture(owned.identity)
    ledger = CaseBudgetLedger()
    async with factory() as session:
        async with session.begin():
            await ledger.create_case_account(
                session,
                workspace_id=owned.identity.workspace_id,
                case_id=owned.identity.case_id,
                currency="CNY",
                limit_amount=limit,
                provider_egress_policy_version=provider_egress_policy_version,
                provider_base_url=provider_base_url,
            )

    turns: list[BoundEpisodeTurn] = []
    version = owned.base["version"]
    if not isinstance(version, AgentVersion):
        raise AssertionError("seed AgentVersion missing")
    threads = ThreadService(factory)
    runtime = AgentRunService(factory)
    for turn_index in range(turn_count):
        opening = f"budget turn {turn_index} {uuid4().hex}"
        reservation = await lifecycle.reserve_turn(
            owned.identity,
            turn_index=turn_index,
            input_hash=hashlib.sha256(opening.encode()).hexdigest(),
        )
        thread = await threads.create_thread(
            owned.context,
            agent_id=version.agent_id,
            title=f"case budget {owned.identity.case_id.hex}",
            thread_id=episode.reserved_thread_id,
        )
        await lifecycle.bind_thread(owned.identity, thread_id=thread.id)
        thread_turn = await threads.open_turn(
            owned.context,
            thread.id,
            user_input=opening,
            client_token=reservation.client_token,
        )
        run = await runtime.prepare_run(
            owned.context,
            agent_version_id=owned.variant.agent_version_id,
            input_text=opening,
            thread_id=thread.id,
            thread_turn_id=thread_turn.id,
            execution_overrides=AgentRunExecutionOverrides.for_evaluation(
                owned.variant.effective_knowledge_snapshots,
                wiki_publications=owned.variant.effective_wiki_publications,
            ),
        )
        await threads.attach_run(owned.context, turn_id=thread_turn.id, run_id=run.id)
        await lifecycle.bind_turn(
            owned.identity,
            turn_index=turn_index,
            turn_id=thread_turn.id,
            run_id=run.id,
        )
        async with factory() as session:
            async with session.begin():
                await ledger.create_case_run_binding(
                    session,
                    workspace_id=owned.identity.workspace_id,
                    case_id=owned.identity.case_id,
                    turn_index=turn_index,
                    evaluation_run_id=owned.identity.run_id,
                    agent_run_id=run.id,
                    dispatch_owner=owned.identity.owner,
                    dispatch_generation=owned.identity.generation,
                )
                await ledger.create_account(
                    session,
                    workspace_id=owned.identity.workspace_id,
                    run_id=run.id,
                    currency="CNY",
                    limit_amount=limit,
                    case_id=owned.identity.case_id,
                )
        turns.append(
            BoundEpisodeTurn(
                thread=thread,
                turn=thread_turn,
                reservation=reservation,
                run=run,
            )
        )
    return BudgetCaseFixture(owned=owned, episode=episode, turns=tuple(turns))


async def _reserve(
    factory: async_sessionmaker[AsyncSession],
    fixture: BudgetCaseFixture,
    run_index: int,
    *,
    attempt_identity: str,
    case_attempt_identity: str,
    amount: Decimal,
) -> object:
    ledger = CaseBudgetLedger()
    async with factory() as session:
        async with session.begin():
            return await ledger.reserve(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[run_index].run.id,
                attempt_identity=attempt_identity,
                case_attempt_identity=case_attempt_identity,
                basis_identity="pricing:fixture-v1",
                attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                amount=amount,
                currency="CNY",
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )


@pytest.mark.asyncio
async def test_legacy_null_and_case_binding_contract(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch)
    async with factory() as session:
        account_version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert account_version == "0053_interactive_case_budget"
        legacy_run_id = await _create_bare_run(session, fixture.owned)
        await session.commit()
        ledger = RunBudgetLedger()
        async with session.begin():
            await ledger.create_account(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=legacy_run_id,
                currency="CNY",
                limit_amount=Decimal("5"),
            )
            reserved = await ledger.reserve(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=legacy_run_id,
                attempt_identity="legacy-attempt",
                attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                amount=Decimal("1"),
                currency="CNY",
                reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
            )
            assert reserved.account.case_id is None
            assert reserved.attempt.case_id is None
            assert reserved.attempt.case_attempt_identity is None
            assert reserved.attempt.basis_identity is None
        unbound_run_id = await _create_bare_run(session, fixture.owned)
        await session.commit()
        case_ledger = CaseBudgetLedger()
        async with session.begin():
            await case_ledger.create_account(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=unbound_run_id,
                currency="CNY",
                limit_amount=Decimal("5"),
                case_id=fixture.owned.identity.case_id,
            )
            with pytest.raises(RunBudgetLedgerError) as unbound:
                await case_ledger.reserve(
                    session,
                    workspace_id=fixture.owned.identity.workspace_id,
                    run_id=unbound_run_id,
                    attempt_identity="unbound-attempt",
                    case_attempt_identity="turn-unbound",
                    basis_identity="pricing:fixture-v1",
                    attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                    amount=Decimal("1"),
                    currency="CNY",
                    reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
                )
            assert unbound.value.code == "CASE_BUDGET_RUN_BINDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_two_runs_serialize_on_parent_case_balance(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch, limit=Decimal("3"))

    async def reserve(index: int, identity: str):
        return await _reserve(
            factory,
            fixture,
            index,
            attempt_identity=identity,
            case_attempt_identity=identity,
            amount=Decimal("2"),
        )

    results = await asyncio.gather(
        reserve(0, "turn-0:round-1"),
        reserve(1, "turn-1:round-1"),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) == 1 and isinstance(failures[0], RunBudgetLedgerError)
    assert failures[0].code == "CASE_BUDGET_INSUFFICIENT_FUNDS"
    async with factory() as session:
        case_account = await ledger_case_account(session, fixture)
        assert case_account.reserved_amount == Decimal("2.00000000")


async def ledger_case_account(session: AsyncSession, fixture: BudgetCaseFixture):
    account = await session.scalar(
        select(CaseBudgetAccount).where(
            CaseBudgetAccount.workspace_id == fixture.owned.identity.workspace_id,
            CaseBudgetAccount.case_id == fixture.owned.identity.case_id,
        )
    )
    assert account is not None
    return account


@pytest.mark.asyncio
async def test_parent_child_rollback_and_cross_tenant_fk(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch, limit=Decimal("5"), turn_count=1)
    ledger = CaseBudgetLedger()
    try:
        async with factory() as session:
            async with session.begin():
                await ledger.reserve(
                    session,
                    workspace_id=fixture.owned.identity.workspace_id,
                    run_id=fixture.turns[0].run.id,
                    attempt_identity="rollback-attempt",
                    case_attempt_identity="turn-0:rollback",
                    basis_identity="pricing:fixture-v1",
                    attempt_kind=RunBudgetAttemptKind.MAIN_MODEL,
                    amount=Decimal("1"),
                    currency="CNY",
                    reservation_basis=RunBudgetReservationBasis.CONSERVATIVE_MAXIMUM,
                )
                raise RuntimeError("inject rollback")
    except RuntimeError:
        pass
    async with factory() as session:
        parent = await ledger_case_account(session, fixture)
        child = await session.get(
            RunBudgetAccount,
            {
                "workspace_id": fixture.owned.identity.workspace_id,
                "run_id": fixture.turns[0].run.id,
            },
        )
        assert parent.reserved_amount == Decimal("0.00000000")
        assert child is not None and child.reserved_amount == Decimal("0.00000000")
        assert not await session.scalar(
            select(RunBudgetAttempt).where(
                RunBudgetAttempt.workspace_id == fixture.owned.identity.workspace_id,
                RunBudgetAttempt.run_id == fixture.turns[0].run.id,
            )
        )

    other_owned = (await create_owned_lifecycle_cases(factory, workspace_count=1, repetitions=1))[0]
    async with factory() as session:
        other_run_id = await _create_bare_run(session, other_owned)
        await session.commit()
    with pytest.raises(RunBudgetLedgerError) as cross_tenant:
        async with factory() as session:
            async with session.begin():
                await ledger.create_account(
                    session,
                    workspace_id=other_owned.identity.workspace_id,
                    run_id=other_run_id,
                    currency="CNY",
                    limit_amount=Decimal("1"),
                    case_id=fixture.owned.identity.case_id,
                )
    assert cross_tenant.value.code == "CASE_BUDGET_ACCOUNT_NOT_FOUND"
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                insert(CaseBudgetAccount.__table__).values(
                    workspace_id=other_owned.identity.workspace_id,
                    case_id=fixture.owned.identity.case_id,
                    experiment_variant_id=other_owned.variant.id,
                    currency="CNY",
                    limit_amount=Decimal("1"),
                    reserved_amount=Decimal("0"),
                    settled_amount=Decimal("0"),
                    pricing_snapshot_id=other_owned.variant.pricing_snapshot_id,
                    pricing_snapshot_hash=other_owned.variant.pricing_snapshot_hash,
                    provider="fake",
                    model="frozen-model-a",
                    provider_egress_policy_version="provider_egress_test_v1",
                    provider_base_url=None,
                    budget_policy_version="interactive_case_budget_v1",
                )
            )
            await session.commit()
        await session.rollback()


async def _create_bare_run(session: AsyncSession, owned: OwnedLifecycleCase) -> UUID:
    # create_owned_lifecycle_cases has no AgentRun until a turn is prepared.
    run = AgentRun(
        workspace_id=owned.identity.workspace_id,
        agent_version_id=owned.variant.agent_version_id,
        input_text="cross-tenant budget placeholder",
        created_by=owned.run.created_by,
        resolved_spec_hash=owned.variant.resolved_spec_hash,
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    return run.id


@pytest.mark.asyncio
async def test_same_run_replay_and_cross_run_replay(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch, limit=Decimal("5"))
    first = await _reserve(
        factory,
        fixture,
        0,
        attempt_identity="same-attempt",
        case_attempt_identity="turn-0:round-1",
        amount=Decimal("1"),
    )
    repeated = await _reserve(
        factory,
        fixture,
        0,
        attempt_identity="same-attempt",
        case_attempt_identity="turn-0:round-1",
        amount=Decimal("1"),
    )
    assert first.created is True and repeated.created is False
    with pytest.raises(RunBudgetLedgerError) as replay:
        await _reserve(
            factory,
            fixture,
            1,
            attempt_identity="cross-run-attempt",
            case_attempt_identity="turn-0:round-1",
            amount=Decimal("1"),
        )
    assert replay.value.code == "RUN_BUDGET_CASE_ATTEMPT_REPLAYED"


@pytest.mark.asyncio
async def test_unknown_and_success_observation_keep_holds(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch, limit=Decimal("2"))
    ledger = CaseBudgetLedger()
    await _reserve(
        factory,
        fixture,
        0,
        attempt_identity="hold-attempt",
        case_attempt_identity="turn-0:round-1",
        amount=Decimal("1"),
    )
    with pytest.raises(RunBudgetLedgerError) as non_bill:
        async with factory() as session:
            async with session.begin():
                await ledger.settle(
                    session,
                    workspace_id=fixture.owned.identity.workspace_id,
                    run_id=fixture.turns[0].run.id,
                    attempt_identity="hold-attempt",
                    amount=Decimal("0.1"),
                    currency="CNY",
                    evidence=RunBudgetSettlementEvidence.RESPONSE_ESTIMATE,
                    evidence_identity="estimate:not-a-bill",
                )
    assert non_bill.value.code == "RUN_BUDGET_FINAL_SETTLEMENT_REQUIRES_PROVIDER_BILL"
    async with factory() as session:
        async with session.begin():
            observed, created = await ledger.record_response_observation(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[0].run.id,
                attempt_identity="hold-attempt",
                observation_identity="response-1",
                provider="fake",
                model="frozen-model-a",
                provider_egress_policy_version="provider_egress_test_v1",
                provider_base_url=None,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                cached_tokens=0,
                cost_amount=Decimal("0.01"),
                cost_currency="CNY",
            )
            assert created is True and observed.cost_amount == Decimal("0.01000000")
    async with factory() as session:
        async with session.begin():
            repeated, created = await ledger.record_response_observation(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[0].run.id,
                attempt_identity="hold-attempt",
                observation_identity="response-1",
                provider="fake",
                model="frozen-model-a",
                provider_egress_policy_version="provider_egress_test_v1",
                provider_base_url=None,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                cached_tokens=0,
                cost_amount=Decimal("0.01"),
                cost_currency="CNY",
            )
            assert created is False and repeated.observation_identity == "response-1"
            attempt = await ledger.get_attempt(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[0].run.id,
                attempt_identity="hold-attempt",
            )
            assert attempt.status is RunBudgetAttemptStatus.RESERVED
            await ledger.mark_unknown_outcome(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[0].run.id,
                attempt_identity="hold-attempt",
            )
    async with factory() as session:
        parent = await ledger_case_account(session, fixture)
        child = await ledger.get_account(
            session,
            workspace_id=fixture.owned.identity.workspace_id,
            run_id=fixture.turns[0].run.id,
        )
        assert parent.reserved_amount == Decimal("1.00000000")
        assert child.reserved_amount == Decimal("1.00000000")
    with pytest.raises(RunBudgetLedgerError) as conflict:
        async with factory() as session:
            async with session.begin():
                await ledger.record_response_observation(
                    session,
                    workspace_id=fixture.owned.identity.workspace_id,
                    run_id=fixture.turns[0].run.id,
                    attempt_identity="hold-attempt",
                    observation_identity="response-1",
                    provider="fake",
                    model="frozen-model-a",
                    provider_egress_policy_version="provider_egress_test_v1",
                    provider_base_url=None,
                    input_tokens=101,
                    output_tokens=20,
                    total_tokens=121,
                    cached_tokens=0,
                    cost_amount=Decimal("0.01"),
                    cost_currency="CNY",
                )
    assert conflict.value.code == "CASE_BUDGET_RESPONSE_OBSERVATION_CONFLICT"
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(CaseBudgetResponseObservation)
                .where(
                    CaseBudgetResponseObservation.workspace_id
                    == fixture.owned.identity.workspace_id
                )
                .values(input_tokens=99)
            )
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_provider_bill_low_and_high_settlement(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch, limit=Decimal("2"))
    ledger = CaseBudgetLedger()
    await _reserve(
        factory,
        fixture,
        0,
        attempt_identity="bill-low",
        case_attempt_identity="turn-0:bill-low",
        amount=Decimal("1"),
    )
    async with factory() as session:
        async with session.begin():
            result = await ledger.settle(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[0].run.id,
                attempt_identity="bill-low",
                amount=Decimal("0.4"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="bill:low",
            )
            assert result.account.settled_amount == Decimal("0.40000000")
    await _reserve(
        factory,
        fixture,
        0,
        attempt_identity="bill-high",
        case_attempt_identity="turn-0:bill-high",
        amount=Decimal("1"),
    )
    async with factory() as session:
        async with session.begin():
            await ledger.settle(
                session,
                workspace_id=fixture.owned.identity.workspace_id,
                run_id=fixture.turns[0].run.id,
                attempt_identity="bill-high",
                amount=Decimal("3"),
                currency="CNY",
                evidence=RunBudgetSettlementEvidence.PROVIDER_BILL,
                evidence_identity="bill:high",
            )
    async with factory() as session:
        parent = await ledger.get_case_account(
            session,
            workspace_id=fixture.owned.identity.workspace_id,
            case_id=fixture.owned.identity.case_id,
        )
        assert parent.is_over_limit is True
        assert parent.reserved_amount == Decimal("0.00000000")
        assert parent.settled_amount == Decimal("3.40000000")
    with pytest.raises(RunBudgetLedgerError) as rejected:
        await _reserve(
            factory,
            fixture,
            1,
            attempt_identity="after-overlimit",
            case_attempt_identity="turn-1:after-overlimit",
            amount=Decimal("0.1"),
        )
    assert rejected.value.code == "CASE_BUDGET_INSUFFICIENT_FUNDS"


@pytest.mark.asyncio
async def test_database_guards_reject_identity_mutation_and_bad_aggregate(
    db_resources, monkeypatch
) -> None:
    _, factory = db_resources
    fixture = await _create_budget_case(factory, monkeypatch, limit=Decimal("5"), turn_count=1)
    await _reserve(
        factory,
        fixture,
        0,
        attempt_identity="guard-attempt",
        case_attempt_identity="turn-0:guard",
        amount=Decimal("1"),
    )
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(CaseBudgetAccount)
                .where(
                    CaseBudgetAccount.workspace_id == fixture.owned.identity.workspace_id,
                    CaseBudgetAccount.case_id == fixture.owned.identity.case_id,
                )
                .values(limit_amount=Decimal("6"))
            )
            await session.commit()
        await session.rollback()
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(RunBudgetAttempt)
                .where(
                    RunBudgetAttempt.workspace_id == fixture.owned.identity.workspace_id,
                    RunBudgetAttempt.run_id == fixture.turns[0].run.id,
                    RunBudgetAttempt.attempt_identity == "guard-attempt",
                )
                .values(case_attempt_identity="changed")
            )
            await session.commit()
        await session.rollback()
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(CaseBudgetAccount)
                .where(
                    CaseBudgetAccount.workspace_id == fixture.owned.identity.workspace_id,
                    CaseBudgetAccount.case_id == fixture.owned.identity.case_id,
                )
                .values(reserved_amount=Decimal("2"))
            )
            await session.commit()
        await session.rollback()
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(RunBudgetAccount)
                .where(
                    RunBudgetAccount.workspace_id == fixture.owned.identity.workspace_id,
                    RunBudgetAccount.run_id == fixture.turns[0].run.id,
                )
                .values(case_id=None)
            )
            await session.commit()
        await session.rollback()
    async with factory() as session:
        observation = await session.scalar(
            select(CaseBudgetResponseObservation).where(
                CaseBudgetResponseObservation.workspace_id == fixture.owned.identity.workspace_id
            )
        )
        assert observation is None
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE case_budget_run_bindings SET dispatch_generation = "
                    "dispatch_generation + 1 WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": fixture.owned.identity.workspace_id},
            )
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_wrong_case_and_duplicate_run_binding_are_rejected(db_resources, monkeypatch) -> None:
    _, factory = db_resources
    first = await _create_budget_case(factory, monkeypatch, limit=Decimal("5"), turn_count=1)
    second = await _create_budget_case(factory, monkeypatch, limit=Decimal("5"), turn_count=1)
    ledger = CaseBudgetLedger()
    with pytest.raises(RunBudgetLedgerError) as wrong_run:
        async with factory() as session:
            async with session.begin():
                await ledger.create_case_run_binding(
                    session,
                    workspace_id=first.owned.identity.workspace_id,
                    case_id=first.owned.identity.case_id,
                    turn_index=0,
                    evaluation_run_id=first.owned.identity.run_id,
                    agent_run_id=second.turns[0].run.id,
                    dispatch_owner=first.owned.identity.owner,
                    dispatch_generation=first.owned.identity.generation,
                )
    assert wrong_run.value.code == "CASE_BUDGET_TURN_BINDING_INVALID"
    with pytest.raises(RunBudgetLedgerError) as duplicate:
        async with factory() as session:
            async with session.begin():
                await ledger.create_case_run_binding(
                    session,
                    workspace_id=first.owned.identity.workspace_id,
                    case_id=first.owned.identity.case_id,
                    turn_index=0,
                    evaluation_run_id=first.owned.identity.run_id,
                    agent_run_id=first.turns[0].run.id,
                    dispatch_owner=first.owned.identity.owner + "-different",
                    dispatch_generation=first.owned.identity.generation + 1,
                )
    assert duplicate.value.code == "CASE_BUDGET_RUN_BINDING_CONFLICT"
