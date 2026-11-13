"""The formal worker and SQL gateway with a local, synthetic provider billing contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from apps.worker.tasks import evaluation as worker
from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.run_budget_ledger import RunBudgetAccount, RunBudgetAttempt
from packages.agent_runtime.runtime import AgentRunService
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.case_budget_ledger import CaseBudgetLedger
from packages.evaluation.case_budget_models import (
    CaseBudgetAccount,
    CaseBudgetResponseObservation,
    CaseBudgetRunBinding,
)
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.model_gateway.contracts import CostEstimate, ModelResponse, ModelUsage
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ProviderCredential
from packages.threads.service import ThreadService
from tests.integration.test_case_budget_gateway import (
    ROUTE_BASE_URL,
    ROUTE_MODEL,
    ROUTE_PROVIDER,
    _install_new_published_route,
)
from tests.integration.test_case_budget_ledger import (
    _cny_pricing,
    db_resources,
    migrated_database,
    pytestmark,
)
from tests.support import ae6b_lifecycle

__all__ = ["db_resources", "migrated_database", "pytestmark"]

MAX_INPUT = 8192
HOLD = Decimal("0.00082080")  # 8192 synthetic input tokens at .10, 8 output at .20 / million.


def _item(cap: str | None, *, ordinal: int = 0):
    item = ae6b_lifecycle.interactive_item(
        f"worker-budget-{uuid4().hex}",
        ordinal=ordinal,
        assertions={"min_turns": 2, "ticket_count": 0},
    )
    if cap is not None:
        item["input"]["max_cost_cny"] = cap
    return item


async def _worker_case(factory, monkeypatch, tmp_path, *, cap, mismatch=None):
    _install_new_published_route(monkeypatch)
    monkeypatch.setattr(ae6b_lifecycle, "_pricing", _cny_pricing)
    # The run-level preflight must reject a later capped item before the earlier
    # uncapped item reaches the gateway.
    items = [_item(None), _item(cap, ordinal=1)] if mismatch else [_item(cap)]
    if mismatch == "approval_script":
        items[-1]["expected"]["approval_decisions"] = ["APPROVED"]
    setup = await ae6b_lifecycle.create_interactive_driver_experiment(factory, items=items)
    workspace = setup.run.workspace_id
    if mismatch == "invalid_current_route":
        async with factory() as session, session.begin():
            current = await session.get(ProviderCredential, setup.base["credential"].id)
            current.base_url = "invalid-route"
    contract = {
        "workspace_id": str(workspace),
        "pricing_snapshot_id": str(setup.variant.pricing_snapshot_id),
        "pricing_snapshot_hash": setup.variant.pricing_snapshot_hash,
        "provider": ROUTE_PROVIDER,
        "model": ROUTE_MODEL,
        "base_url": "https://off-route.invalid/v1" if mismatch == "route" else ROUTE_BASE_URL,
        "currency": "CNY",
        "input_price_per_1m": "0.10",
        "output_price_per_1m": "0.20",
        "cached_input_price_per_1m": None,
        "review_reference": "Synthetic local fixture, no real provider tariff.",
        "valid_until": (
            datetime.now(UTC) + timedelta(days=-1 if mismatch == "expired" else 1)
        ).isoformat(),
        "billing_terms": "text_tokens_only_including_failed_requests_v1",
        "request_mode": "provider_default",
        "max_billable_input_tokens": MAX_INPUT,
        "max_billable_output_tokens": 8,
        "input_bound": "utf8_bytes_plus_attested_framing_v1",
        "tokens_per_utf8_byte": 1,
        "tokens_per_message": 4,
        "tokens_per_request": 8,
    }
    path = tmp_path / "synthetic-contract.json"
    contracts = [contract]
    if mismatch == "ambiguous":
        contracts.append({**contract, "review_reference": "Second synthetic contract"})
    path.write_text(json.dumps({"schema_version": 1, "contracts": contracts}), encoding="utf-8")
    settings = Settings(
        testing=True,
        evaluation_sandbox_workspace_ids={workspace},
        evaluation_case_budget_attestations_path=None if mismatch == "missing" else path,
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    return setup


class _Provider:
    def __init__(self, factory, workspace, *, uncertain=False):
        self.factory = factory
        self.workspace = workspace
        self.uncertain = uncertain
        self.calls = 0

    async def complete(self, profile, credential, request):
        self.calls += 1
        assert profile.model == ROUTE_MODEL and credential.base_url == ROUTE_BASE_URL
        async with self.factory() as session:
            attempts = list(
                await session.scalars(
                    select(RunBudgetAttempt).where(RunBudgetAttempt.workspace_id == self.workspace)
                )
            )
            parent = await session.scalar(
                select(CaseBudgetAccount).where(CaseBudgetAccount.workspace_id == self.workspace)
            )
            assert len(attempts) == self.calls
            assert parent.reserved_amount == self.calls * HOLD
            assert all(
                attempt.status == "RESERVED" and attempt.reserved_amount == HOLD
                for attempt in attempts
            )
        if self.uncertain:
            raise TimeoutError()
        return ModelResponse(
            content="Please provide your customer reference.",
            provider=ROUTE_PROVIDER,
            model=ROUTE_MODEL,
            usage=ModelUsage(input_tokens=12, output_tokens=6, total_tokens=18, cached_tokens=0),
            cost_estimate=CostEstimate(Decimal("0.000000001"), "USD"),
        )


def _install_provider(monkeypatch, provider):
    def gateway(session, *, settings, trace_sink):
        return SqlAlchemyModelGateway(session, adapter=provider, trace_sink=trace_sink)

    monkeypatch.setattr(worker, "_production_model_gateway", gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize("turns_funded", [1, 2])
async def test_formal_worker_reserves_per_turn_and_blocks_unbudgeted_reentry(
    db_resources, monkeypatch, tmp_path, turns_funded
):
    _, factory = db_resources
    setup = await _worker_case(factory, monkeypatch, tmp_path, cap=str(HOLD * turns_funded))
    provider = _Provider(factory, setup.run.workspace_id)
    _install_provider(monkeypatch, provider)
    dispatch = InteractiveLifecycle.dispatch

    async def assert_other_executors_denied(lifecycle, identity, **kwargs):
        # This happens after preparation but before DISPATCH_INTENT. The child
        # account must already be visible, even to the generic Run worker.
        async with factory() as session:
            turn = await session.scalar(
                select(InteractiveEpisodeTurn).where(
                    InteractiveEpisodeTurn.workspace_id == identity.workspace_id,
                    InteractiveEpisodeTurn.case_id == identity.case_id,
                    InteractiveEpisodeTurn.turn_index == kwargs["turn_index"],
                )
            )
            run = await session.get(AgentRun, turn.agent_run_id)
            assert await session.get(RunBudgetAccount, (identity.workspace_id, run.id)) is not None
        with pytest.raises(AgentHubError, match="bound case budget") as blocked:
            await AgentRunService(factory).execute_prepared_run(setup.context, run_id=run.id)
        assert blocked.value.code == "RUN_BUDGET_BINDING_UNAVAILABLE"
        with pytest.raises(AgentHubError) as rejected:
            await ThreadService(factory, run_service=AgentRunService(factory)).submit_turn(
                setup.context,
                run.thread_id,
                user_input=run.input_text,
                client_token=turn.client_token,
            )
        assert rejected.value.code == "INTERACTIVE_CASE_EXECUTOR_REQUIRED"
        await dispatch(lifecycle, identity, **kwargs)

    monkeypatch.setattr(InteractiveLifecycle, "dispatch", assert_other_executors_denied)
    await worker._execute_experiment_run(setup.run.id)
    await worker._execute_experiment_run(setup.run.id)
    async with factory() as session:
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.experiment_run_id == setup.run.id
            )
        )
        assert case.status == "SUCCEEDED", (case.failure_code, case.observation)
        assert case.observation["executed_turn_count"] == 2
        assert case.observation["stop_reason"] == (
            "budget_exhausted" if turns_funded == 1 else "goal_met"
        )
        parent = await session.get(CaseBudgetAccount, (case.workspace_id, case.id))
        assert parent.reserved_amount == parent.limit_amount == HOLD * turns_funded
        assert parent.settled_amount == 0
        bindings = list(
            await session.scalars(
                select(CaseBudgetRunBinding).where(CaseBudgetRunBinding.case_id == case.id)
            )
        )
        assert len(bindings) == 2
        children = list(
            await session.scalars(
                select(RunBudgetAccount).where(RunBudgetAccount.case_id == case.id)
            )
        )
        assert {row.agent_run_id for row in bindings} == {child.run_id for child in children}
        assert sum(child.reserved_amount for child in children) == parent.reserved_amount
        rows = list(
            await session.scalars(
                select(CaseBudgetResponseObservation).where(
                    CaseBudgetResponseObservation.case_id == case.id
                )
            )
        )
        assert len(rows) == provider.calls == turns_funded
        assert all(
            row.cost_currency == "USD" and row.cost_amount == Decimal("0.000000001") for row in rows
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mismatch", "reason"),
    [
        ("route", "BILLING_ROUTE_MISMATCH"),
        ("invalid_current_route", "BILLING_ROUTE_OR_PRICE_MISMATCH"),
        ("missing", "BILLING_PROOF_MISSING"),
        ("expired", "BILLING_PROOF_EXPIRED"),
        ("ambiguous", "BILLING_PROOF_AMBIGUOUS"),
        ("approval_script", "INTERACTIVE_CASE_BUDGET_APPROVAL_UNSUPPORTED"),
        ("first_quote_exceeds_cap", "BILLING_FIRST_QUOTE_EXCEEDS_CASE_LIMIT"),
    ],
)
async def test_all_plan_items_are_preflighted_before_any_dispatch(
    db_resources, monkeypatch, tmp_path, mismatch, reason
):
    _, factory = db_resources
    cap = "0.00000001" if mismatch == "first_quote_exceeds_cap" else "1.00"
    setup = await _worker_case(factory, monkeypatch, tmp_path, cap=cap, mismatch=mismatch)
    provider = _Provider(factory, setup.run.workspace_id)
    _install_provider(monkeypatch, provider)
    await worker._execute_experiment_run(setup.run.id)
    async with factory() as session:
        run = await session.get(EvaluationExperimentRun, setup.run.id)
        cases = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.experiment_run_id == run.id
                )
            )
        )
        assert len(cases) == 2 and all(row.status == "FAILED" for row in cases)
        assert run.failure_code == "INTERACTIVE_COST_RESERVATION_UNAVAILABLE"
        assert all(row.failure_code == run.failure_code for row in cases)
        assert reason in run.safe_failure_message
        assert all(row.safe_failure_message == run.safe_failure_message for row in cases)
        assert not await session.scalar(
            select(InteractiveEpisode.case_id).where(
                InteractiveEpisode.workspace_id == run.workspace_id
            )
        )
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["after_commit", "timeout", "observation_write", "unknown_write"])
async def test_lost_fence_and_unknown_outcome_retain_budget_without_replay(
    db_resources, monkeypatch, tmp_path, loss
):
    _, factory = db_resources
    setup = await _worker_case(factory, monkeypatch, tmp_path, cap="1.00")
    provider = _Provider(
        factory, setup.run.workspace_id, uncertain=loss in {"timeout", "unknown_write"}
    )
    _install_provider(monkeypatch, provider)
    if loss == "after_commit":
        reserve = RunBudgetAttemptAdapter.reserve_attempt

        async def expire_owner(adapter, *args, **kwargs):
            hold = await reserve(adapter, *args, **kwargs)
            async with factory() as session, session.begin():
                run = await session.get(AgentRun, hold.run_id)
                run.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            return hold

        monkeypatch.setattr(RunBudgetAttemptAdapter, "reserve_attempt", expire_owner)
    elif loss in {"observation_write", "unknown_write"}:

        async def fail_evidence_write(*args, **kwargs):
            raise RuntimeError("simulated database failure after dispatch")

        method = (
            "record_response_observation" if loss == "observation_write" else "mark_unknown_outcome"
        )
        monkeypatch.setattr(CaseBudgetLedger, method, fail_evidence_write)
    await worker._execute_experiment_run(setup.run.id)
    await worker._execute_experiment_run(setup.run.id)
    async with factory() as session:
        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.experiment_run_id == setup.run.id
            )
        )
        turn = await session.scalar(
            select(InteractiveEpisodeTurn).where(InteractiveEpisodeTurn.case_id == case.id)
        )
        run = await session.get(AgentRun, case.agent_run_id)
        assert case.failure_code == "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
        assert run.status == "NEEDS_ATTENTION" and turn.dispatch_status == "UNKNOWN_OUTCOME"
        attempts = list(
            await session.scalars(
                select(RunBudgetAttempt).where(RunBudgetAttempt.case_id == case.id)
            )
        )
        assert len(attempts) == 1
        assert attempts[0].status == ("UNKNOWN_OUTCOME" if loss == "timeout" else "RESERVED")
        parent = await session.get(CaseBudgetAccount, (case.workspace_id, case.id))
        assert parent.reserved_amount == HOLD and parent.settled_amount == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CaseBudgetResponseObservation)
                .where(CaseBudgetResponseObservation.case_id == case.id)
            )
            == 0
        )
    assert provider.calls == (0 if loss == "after_commit" else 1)
