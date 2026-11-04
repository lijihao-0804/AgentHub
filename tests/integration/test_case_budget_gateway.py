"""Actual model-gateway dispatch through a real Interactive case budget."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import Agent, AgentRun, AgentVersion
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.run_budget_ledger import RunBudgetAccount, RunBudgetAttempt
from packages.evaluation.case_budget_adapter import CaseBudgetAttemptAdapter
from packages.evaluation.case_budget_ledger import CaseBudgetLedger
from packages.evaluation.case_budget_models import (
    CaseBudgetAccount,
    CaseBudgetResponseObservation,
)
from packages.evaluation.case_budget_pricing import (
    SUPPORTED_FEE_COVERAGE,
    BillingAttestation,
    BillingAttestationRegistry,
    FrozenProviderRoute,
    RequestAdmissibilityEvidence,
)
from packages.evaluation.interactive_lifecycle import InteractiveLifecycle
from packages.evaluation.models import PricingSnapshot
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    AliyunDeepSeekRequestMode,
    CostEstimate,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.run_budget_adapter import ModelAttemptBudgetError
from tests.integration.test_case_budget_ledger import (
    BudgetCaseFixture,
    _create_budget_case,
)
from tests.integration.test_case_budget_ledger import (
    db_resources as db_resources,
)
from tests.integration.test_case_budget_ledger import (
    migrated_database as migrated_database,
)
from tests.integration.test_case_budget_ledger import (
    pytestmark as pytestmark,
)
from tests.integration.test_m7b_experiments import _manager_context

ROUTE_PROVIDER = "openai-compatible"
ROUTE_MODEL = "frozen-model-a"
ROUTE_BASE_URL = "https://provider.invalid/v1"
EXECUTION_OWNER = "synthetic-case-gateway-worker"
VALIDATOR_ID = "synthetic-exact-request-validator-v1"
ATTESTATION_ID = "synthetic-openai-compatible-proof-v1"
CASE_LIMIT = Decimal("0.00000400")
EXPECTED_HOLD = Decimal("0.00000320")


class _ExactRequestValidator:
    """Synthetic test evidence for one exact request; not provider evidence."""

    identity = VALIDATOR_ID

    def __init__(self, request: ModelRequest, *, max_input_tokens: int) -> None:
        self.request = request
        self.max_input_tokens = max_input_tokens
        self.calls: list[tuple[ModelRequest, int]] = []

    def validate(
        self,
        request: ModelRequest,
        *,
        max_billable_input_tokens: int,
    ) -> bool:
        self.calls.append((request, max_billable_input_tokens))
        return request == self.request and max_billable_input_tokens == self.max_input_tokens


class _FakeProvider:
    def __init__(self, *, on_call=None, delay: float = 0) -> None:
        self.calls: list[str] = []
        self.on_call = on_call
        self.delay = delay

    async def complete(self, profile, credential, request) -> ModelResponse:
        del credential, request
        self.calls.append(profile.model)
        if self.on_call is not None:
            await self.on_call()
        if self.delay:
            await asyncio.sleep(self.delay)
        return ModelResponse(
            content="synthetic gateway response",
            provider=ROUTE_PROVIDER,
            model=profile.model,
            usage=ModelUsage(input_tokens=8, output_tokens=4, total_tokens=12),
            cost_estimate=CostEstimate(Decimal("0.00000001"), "CNY"),
        )

    async def health(self, profile, credential) -> None:
        del profile, credential

    async def stream(self, profile, credential, request):
        del profile, credential, request
        raise AssertionError("gateway integration uses synchronous generation only")


def request_for(prompt: str) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content=prompt),),
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
    )


def _install_new_published_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_seconds: Decimal = Decimal("1"),
) -> None:
    import tests.support.ae6b_lifecycle as lifecycle_support

    original_seed = lifecycle_support._seed

    async def seeded_with_route(session, *, label: str, **kwargs):
        base = await original_seed(session, label=label, **kwargs)
        workspace_id = base["workspace_id"]
        credential = ProviderCredential(
            workspace_id=workspace_id,
            provider=ROUTE_PROVIDER,
            name=f"synthetic-openai-compatible-{uuid4().hex}",
            secret="synthetic-not-a-real-secret",
            base_url=ROUTE_BASE_URL,
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace_id,
            provider_credential_id=credential.id,
            model=ROUTE_MODEL,
            temperature=Decimal("0"),
            max_tokens=8,
            timeout_seconds=timeout_seconds,
            capabilities={
                "tool_calling": True,
                "streaming": False,
                "structured_output": False,
                "vision": False,
                "max_context_tokens": 8_192,
            },
        )
        session.add(profile)
        await session.flush()
        agent = await session.get(Agent, base["version"].agent_id)
        assert agent is not None
        agent.model_profile_id = profile.id
        agent.model_retry_policy = {"max_attempts": 1}
        await session.flush()
        published = await AgentPublishService().publish(
            session,
            _manager_context(base),
            agent.id,
        )
        version = await session.get(AgentVersion, published.id)
        assert version is not None
        base["credential"] = credential
        base["profile"] = profile
        base["version"] = version
        base["pricing_provider"] = ROUTE_PROVIDER
        return base

    monkeypatch.setattr(lifecycle_support, "_seed", seeded_with_route)


def _gateway_context(fixture: BudgetCaseFixture):
    permissions = frozenset((*fixture.owned.context.permissions, "evaluation_run"))
    return fixture.owned.context.model_copy(update={"permissions": permissions})


def _published_plan(fixture: BudgetCaseFixture):
    version = fixture.owned.base["version"]
    assert isinstance(version, AgentVersion)
    spec = parse_frozen_agent_spec(
        version.resolved_spec,
        workspace_id=fixture.owned.identity.workspace_id,
    )
    assert spec.model_plan.primary.provider_egress is not None
    assert spec.model_plan.primary.provider == ROUTE_PROVIDER
    assert spec.model_plan.primary.model == ROUTE_MODEL
    assert spec.model_plan.primary.provider_egress.policy_version == PROVIDER_EGRESS_POLICY_VERSION
    assert spec.model_plan.primary.provider_egress.base_url == ROUTE_BASE_URL
    assert spec.model_plan.fallbacks == ()
    assert spec.model_plan.retry_policy.max_attempts == 1
    return version, spec.model_plan


async def _dispatched_case(
    factory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_seconds: Decimal = Decimal("1"),
) -> BudgetCaseFixture:
    _install_new_published_route(monkeypatch, timeout_seconds=timeout_seconds)
    fixture = await _create_budget_case(
        factory,
        monkeypatch,
        limit=CASE_LIMIT,
        turn_count=2,
        provider_egress_policy_version=PROVIDER_EGRESS_POLICY_VERSION,
        provider_base_url=ROUTE_BASE_URL,
    )
    lifecycle = InteractiveLifecycle(factory)
    for turn_index in range(len(fixture.turns)):
        await lifecycle.dispatch(fixture.owned.identity, turn_index=turn_index)
    async with factory() as session, session.begin():
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id.in_(tuple(turn.run.id for turn in fixture.turns)))
            .values(
                execution_owner=EXECUTION_OWNER,
                execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    return fixture


async def _attestation(
    factory,
    fixture: BudgetCaseFixture,
    monkeypatch: pytest.MonkeyPatch,
    request: ModelRequest,
) -> tuple[BillingAttestation, BillingAttestationRegistry]:
    del monkeypatch
    async with factory() as session:
        account = await CaseBudgetLedger().get_case_account(
            session,
            workspace_id=fixture.owned.identity.workspace_id,
            case_id=fixture.owned.identity.case_id,
        )
        snapshot = await session.scalar(
            select(PricingSnapshot).where(
                PricingSnapshot.workspace_id == account.workspace_id,
                PricingSnapshot.id == account.pricing_snapshot_id,
            )
        )
    assert snapshot is not None
    route = FrozenProviderRoute(
        provider=account.provider,
        model=account.model,
        policy_version=account.provider_egress_policy_version,
        base_url=account.provider_base_url,
    )
    max_input_tokens = 16
    evidence = RequestAdmissibilityEvidence(
        max_billable_input_tokens=max_input_tokens,
        max_billable_output_tokens=8,
        fee_coverage=SUPPORTED_FEE_COVERAGE,
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        input_upper_bound_validator_id=VALIDATOR_ID,
    )
    attestation = BillingAttestation(
        identity=ATTESTATION_ID,
        workspace_id=account.workspace_id,
        route=route,
        pricing_snapshot_id=snapshot.id,
        pricing_snapshot_hash=snapshot.content_hash,
        currency="CNY",
        input_price_per_1m=snapshot.input_price_per_1m,
        output_price_per_1m=snapshot.output_price_per_1m,
        cached_input_price_per_1m=snapshot.cached_input_price_per_1m,
        request_evidence=evidence,
    )
    validator = _ExactRequestValidator(request, max_input_tokens=max_input_tokens)
    registry = BillingAttestationRegistry.server_supplied(
        (attestation,),
        validators={validator.identity: validator},
    )
    return attestation, registry


def _adapter(
    factory,
    fixture: BudgetCaseFixture,
    attestation: BillingAttestation,
    registry: BillingAttestationRegistry,
    *,
    model_round: int,
    turn_index: int = 0,
    identity=None,
    execution_owner: str = EXECUTION_OWNER,
) -> CaseBudgetAttemptAdapter:
    return CaseBudgetAttemptAdapter(
        factory,
        identity=identity or fixture.owned.identity,
        turn_index=turn_index,
        agent_run_id=fixture.turns[turn_index].run.id,
        model_round=model_round,
        execution_owner=execution_owner,
        attestation=attestation,
        registry=registry,
    )


async def _assert_hold(
    factory,
    fixture: BudgetCaseFixture,
    *,
    expected: Decimal = EXPECTED_HOLD,
) -> RunBudgetAttempt:
    async with factory() as session:
        parent = await session.get(
            CaseBudgetAccount,
            {
                "workspace_id": fixture.owned.identity.workspace_id,
                "case_id": fixture.owned.identity.case_id,
            },
        )
        child = await session.get(
            RunBudgetAccount,
            {
                "workspace_id": fixture.owned.identity.workspace_id,
                "run_id": fixture.turns[0].run.id,
            },
        )
        attempt = await session.scalar(
            select(RunBudgetAttempt).where(
                RunBudgetAttempt.workspace_id == fixture.owned.identity.workspace_id,
                RunBudgetAttempt.run_id == fixture.turns[0].run.id,
            )
        )
    assert parent is not None and child is not None and attempt is not None
    assert parent.reserved_amount == expected
    assert child.reserved_amount == expected
    return attempt


@pytest.mark.asyncio
async def test_gateway_commits_hold_before_fake_provider_and_replay_never_calls(
    db_resources,
    monkeypatch,
) -> None:
    _, factory = db_resources
    fixture = await _dispatched_case(factory, monkeypatch)
    request = request_for("synthetic gateway request")
    version, plan = _published_plan(fixture)
    assert version.id == fixture.owned.variant.agent_version_id
    attestation, registry = await _attestation(factory, fixture, monkeypatch, request)
    budget = _adapter(factory, fixture, attestation, registry, model_round=1)

    async def visible_hold() -> None:
        attempt = await _assert_hold(factory, fixture)
        assert attempt.status == "RESERVED"

    provider = _FakeProvider(on_call=visible_hold)
    async with factory() as gateway_session:
        gateway = SqlAlchemyModelGateway(
            gateway_session,
            adapter=provider,
            attempt_budget=budget,
        )
        response = await gateway.generate_resolved(
            _gateway_context(fixture),
            plan,
            request,
        )
    assert provider.calls == [ROUTE_MODEL]
    assert response.provider == ROUTE_PROVIDER
    await budget.record_response(response)
    attempt = await _assert_hold(factory, fixture)
    assert attempt.status == "RESERVED"
    async with factory() as session:
        assert (
            await session.scalar(
                select(CaseBudgetResponseObservation).where(
                    CaseBudgetResponseObservation.run_id == fixture.turns[0].run.id
                )
            )
            is not None
        )

    with pytest.raises(ModelAttemptBudgetError) as replayed:
        async with factory() as replay_session:
            replay_gateway = SqlAlchemyModelGateway(
                replay_session,
                adapter=provider,
                attempt_budget=budget,
            )
            await replay_gateway.generate_resolved(_gateway_context(fixture), plan, request)
    assert replayed.value.code == "CASE_BUDGET_ATTEMPT_RECONCILIATION_REQUIRED"
    assert provider.calls == [ROUTE_MODEL]

    for model_round, identity, owner in (
        (2, fixture.owned.identity, "stale-worker"),
        (
            3,
            replace(fixture.owned.identity, generation=fixture.owned.identity.generation + 1),
            EXECUTION_OWNER,
        ),
    ):
        stale = _adapter(
            factory,
            fixture,
            attestation,
            registry,
            model_round=model_round,
            identity=identity,
            execution_owner=owner,
        )
        with pytest.raises(ModelAttemptBudgetError) as lost:
            async with factory() as gateway_session:
                gateway = SqlAlchemyModelGateway(
                    gateway_session,
                    adapter=provider,
                    attempt_budget=stale,
                )
                await gateway.generate_resolved(_gateway_context(fixture), plan, request)
        assert lost.value.code == "CASE_BUDGET_EXECUTION_FENCE_INVALID"
        assert provider.calls == [ROUTE_MODEL]

    next_run = _adapter(
        factory,
        fixture,
        attestation,
        registry,
        turn_index=1,
        model_round=1,
    )
    with pytest.raises(ModelAttemptBudgetError) as insufficient:
        async with factory() as gateway_session:
            gateway = SqlAlchemyModelGateway(
                gateway_session,
                adapter=provider,
                attempt_budget=next_run,
            )
            await gateway.generate_resolved(_gateway_context(fixture), plan, request)
    assert insufficient.value.code == "CASE_BUDGET_INSUFFICIENT_FUNDS"
    assert provider.calls == [ROUTE_MODEL]


@pytest.mark.asyncio
async def test_gateway_timeout_marks_unknown_without_retry_or_release(
    db_resources,
    monkeypatch,
) -> None:
    _, factory = db_resources
    fixture = await _dispatched_case(
        factory,
        monkeypatch,
        timeout_seconds=Decimal("0.02"),
    )
    request = request_for("synthetic timeout request")
    _, plan = _published_plan(fixture)
    attestation, registry = await _attestation(factory, fixture, monkeypatch, request)
    budget = _adapter(factory, fixture, attestation, registry, model_round=1)
    provider = _FakeProvider(delay=0.1)
    with pytest.raises(ModelGatewayError) as timed_out:
        async with factory() as gateway_session:
            gateway = SqlAlchemyModelGateway(
                gateway_session,
                adapter=provider,
                attempt_budget=budget,
            )
            await gateway.generate_resolved(_gateway_context(fixture), plan, request)
    assert timed_out.value.code is ModelGatewayErrorCode.MODEL_TIMEOUT
    assert provider.calls == [ROUTE_MODEL]
    attempt = await _assert_hold(factory, fixture)
    assert attempt.status == "UNKNOWN_OUTCOME"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["case", "route", "price"])
async def test_case_or_attestation_mismatch_stops_before_provider(
    db_resources,
    monkeypatch,
    mismatch,
) -> None:
    _, factory = db_resources
    fixture = await _dispatched_case(factory, monkeypatch)
    request = request_for("synthetic mismatch request")
    _, plan = _published_plan(fixture)
    attestation, registry = await _attestation(factory, fixture, monkeypatch, request)
    identity = fixture.owned.identity
    if mismatch == "case":
        identity = replace(identity, case_id=uuid4())
    elif mismatch == "route":
        attestation = replace(
            attestation,
            route=replace(
                attestation.route,
                base_url="https://other-provider.invalid/v1",
            ),
        )
        registry = BillingAttestationRegistry.server_supplied(
            (attestation,),
            validators={VALIDATOR_ID: _ExactRequestValidator(request, max_input_tokens=16)},
        )
    else:
        attestation = replace(
            attestation,
            input_price_per_1m=attestation.input_price_per_1m + Decimal("0.01"),
        )
        registry = BillingAttestationRegistry.server_supplied(
            (attestation,),
            validators={VALIDATOR_ID: _ExactRequestValidator(request, max_input_tokens=16)},
        )
    budget = _adapter(
        factory,
        fixture,
        attestation,
        registry,
        model_round=1,
        identity=identity,
    )
    provider = _FakeProvider()
    with pytest.raises(ModelAttemptBudgetError):
        async with factory() as gateway_session:
            gateway = SqlAlchemyModelGateway(
                gateway_session,
                adapter=provider,
                attempt_budget=budget,
            )
            await gateway.generate_resolved(_gateway_context(fixture), plan, request)
    assert provider.calls == []
    async with factory() as session:
        assert (
            await session.scalar(
                select(RunBudgetAttempt).where(
                    RunBudgetAttempt.workspace_id == fixture.owned.identity.workspace_id,
                    RunBudgetAttempt.run_id == fixture.turns[0].run.id,
                )
            )
            is None
        )


__all__ = ["db_resources", "migrated_database"]
