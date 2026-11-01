from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.run_budget_adapter import (
    AttemptCostUpperBound,
    ModelAttemptBudgetError,
    ModelAttemptBudgetReservation,
    ModelAttemptKind,
)


class FakeRepository:
    def __init__(
        self,
        profiles: list[ModelProfile],
        credential: ProviderCredential,
    ) -> None:
        self.profiles = {profile.id: profile for profile in profiles}
        self.credential = credential

    async def get_model_profile(
        self,
        context: WorkspaceExecutionContext,
        profile_id: UUID,
    ) -> ModelProfile | None:
        profile = self.profiles.get(profile_id)
        return profile if profile and str(profile.workspace_id) == context.workspace_id else None

    async def get_provider_credential(
        self,
        context: WorkspaceExecutionContext,
        credential_id: UUID,
    ) -> ProviderCredential | None:
        return (
            self.credential
            if self.credential.id == credential_id
            and str(self.credential.workspace_id) == context.workspace_id
            else None
        )


class RecordingAttemptBudget:
    def __init__(self, events: list[tuple[str, str | None]]) -> None:
        self.events = events
        self.reservations: list[ModelAttemptBudgetReservation] = []
        self.unknown: list[ModelAttemptBudgetReservation] = []
        self.failure_code: str | None = None

    async def reserve_attempt(
        self,
        context: WorkspaceExecutionContext,
        profile,
        request,
        *,
        provider: str,
        operation,
        profile_index: int,
        attempt_index: int,
        attempt_kind: ModelAttemptKind,
    ) -> ModelAttemptBudgetReservation:
        del context, request, provider, operation, profile_index, attempt_index
        self.events.append(("reserve", attempt_kind.value))
        if self.failure_code is not None:
            raise ModelAttemptBudgetError(
                self.failure_code,
                "The Run budget rejected the provider attempt.",
            )
        reservation = ModelAttemptBudgetReservation(
            workspace_id=profile.workspace_id,
            run_id=uuid4(),
            attempt_identity=f"model:round-1:{attempt_kind.value}",
            attempt_kind=attempt_kind,
            cost_upper_bound=AttemptCostUpperBound(
                Decimal("1"),
                "CNY",
                "pricing:v1",
            ),
        )
        self.reservations.append(reservation)
        return reservation

    async def mark_unknown_outcome(
        self,
        reservation: ModelAttemptBudgetReservation,
    ) -> None:
        self.events.append(("unknown", reservation.attempt_kind.value))
        self.unknown.append(reservation)


class RecordingProvider:
    def __init__(self, events: list[tuple[str, str | None]]) -> None:
        self.events = events
        self.complete_outcomes: dict[str, list[ModelResponse | ModelGatewayError]] = {}
        self.stream_outcomes: dict[str, list[str]] = {}

    async def complete(
        self,
        profile,
        credential,
        request,
    ) -> ModelResponse:
        del credential, request
        self.events.append(("provider.complete", profile.model))
        outcome = self.complete_outcomes[profile.model].pop(0)
        if isinstance(outcome, ModelGatewayError):
            raise outcome
        return outcome

    async def stream(
        self,
        profile,
        credential,
        request,
    ) -> AsyncIterator[ModelStreamEvent]:
        del credential, request
        self.events.append(("provider.stream", profile.model))
        outcome = self.stream_outcomes[profile.model].pop(0)
        if outcome == "pre-visible-error":
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        if outcome == "post-visible-error":
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta="partial",
            )
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_TIMEOUT,
                retryable=True,
            )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta=profile.model,
        )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=response(profile.model),
        )

    async def health(self, profile, credential) -> None:
        del profile, credential


class FakeTransaction:
    def __init__(self, factory: FakeSessionFactory) -> None:
        self.factory = factory

    async def __aenter__(self):
        self.factory.events.append("begin")
        return self

    async def __aexit__(self, error_type, error, traceback) -> None:
        self.factory.events.append("rollback" if error_type is not None else "commit")


class FakeSession:
    def __init__(self, factory: FakeSessionFactory) -> None:
        self.factory = factory

    async def __aenter__(self):
        self.factory.events.append("session.enter")
        return self

    async def __aexit__(self, error_type, error, traceback) -> None:
        self.factory.events.append("session.exit")

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self.factory)


class FakeSessionFactory:
    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self) -> FakeSession:
        return FakeSession(self)


class FakePriceSource:
    def __init__(self, quote: AttemptCostUpperBound | None = None) -> None:
        self.quote = quote
        self.calls: list[dict] = []

    async def resolve(
        self,
        session,
        *,
        profile,
        request,
        provider,
        attempt_kind,
    ):
        self.calls.append(
            {
                "session": session,
                "profile": profile,
                "request": request,
                "provider": provider,
                "attempt_kind": attempt_kind,
            }
        )
        return self.quote


class FakeRunBudgetLedger:
    def __init__(self, *, created: bool = True) -> None:
        self.created = created
        self.reserve_calls: list[dict] = []
        self.unknown_calls: list[dict] = []

    async def reserve(self, session, **kwargs):
        self.reserve_calls.append({"session": session, **kwargs})
        return SimpleNamespace(created=self.created)

    async def mark_unknown_outcome(self, session, **kwargs):
        self.unknown_calls.append({"session": session, **kwargs})
        return SimpleNamespace(status="UNKNOWN_OUTCOME")


def context_for(workspace_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="run-budget-gateway", trace_id="run-budget"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
    )


def setup_chain() -> tuple[
    WorkspaceExecutionContext,
    FakeRepository,
    list[ModelProfile],
]:
    workspace_id = uuid4()
    credential = ProviderCredential(
        id=uuid4(),
        workspace_id=workspace_id,
        provider="test-provider",
        name="test",
        secret="not-for-logs",
    )
    fallback = ModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model="fallback",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities={"streaming": True, "max_context_tokens": 8192},
    )
    primary = ModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model="primary",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        fallback_profile_id=fallback.id,
        capabilities={"streaming": True, "max_context_tokens": 8192},
    )
    return (
        context_for(workspace_id),
        FakeRepository([primary, fallback], credential),
        [primary, fallback],
    )


def request(*, retry_attempts: int = 1) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        retry_policy=RetryPolicy(max_attempts=retry_attempts),
    )


def response(model: str) -> ModelResponse:
    return ModelResponse(
        content=model,
        provider="test-provider",
        model=model,
    )


def frozen_plan(profiles: list[ModelProfile]) -> ResolvedModelExecutionPlan:
    frozen = [
        ResolvedModelExecutionProfile(
            id=profile.id,
            workspace_id=profile.workspace_id,
            provider_credential_id=profile.provider_credential_id,
            provider="test-provider",
            model=profile.model,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            timeout_seconds=profile.timeout_seconds,
            capabilities=ModelCapabilities(
                streaming=True,
                max_context_tokens=8192,
            ),
        )
        for profile in profiles
    ]
    return ResolvedModelExecutionPlan(
        primary=frozen[0],
        fallbacks=tuple(frozen[1:]),
        retry_policy=RetryPolicy(max_attempts=1),
    )


@pytest.mark.asyncio
async def test_run_budget_adapter_commits_reservation_before_returning() -> None:
    context, _, profiles = setup_chain()
    workspace_id = profiles[0].workspace_id
    run_id = uuid4()
    sessions = FakeSessionFactory()
    quote = AttemptCostUpperBound(Decimal("2.50"), "CNY", "price-book:v1")
    source = FakePriceSource(quote)
    ledger = FakeRunBudgetLedger()
    adapter = RunBudgetAttemptAdapter(
        sessions,
        workspace_id=workspace_id,
        run_id=run_id,
        call_identity="run-1:round-3",
        price_source=source,
        ledger=ledger,
    )
    assert adapter.is_bound_to_run(workspace_id, run_id, "run-1:round-3")
    assert not adapter.is_bound_to_run(workspace_id, uuid4(), "run-1:round-3")
    assert not adapter.is_bound_to_run(workspace_id, run_id, "run-1:round-4")

    reservation = await adapter.reserve_attempt(
        context,
        profiles[0],
        request(),
        provider="test-provider",
        operation="generate",
        profile_index=0,
        attempt_index=0,
        attempt_kind=ModelAttemptKind.MAIN_MODEL,
    )

    assert sessions.events == [
        "session.enter",
        "begin",
        "commit",
        "session.exit",
    ]
    assert len(source.calls) == 1
    assert source.calls[0]["provider"] == "test-provider"
    assert source.calls[0]["attempt_kind"] is ModelAttemptKind.MAIN_MODEL
    assert ledger.reserve_calls[0]["attempt_identity"] == ("model:run-1:round-3:generate:0:0")
    assert ledger.reserve_calls[0]["amount"] == Decimal("2.50")
    assert ledger.reserve_calls[0]["currency"] == "CNY"
    assert reservation.workspace_id == workspace_id
    assert reservation.run_id == run_id
    assert reservation.cost_upper_bound == quote


@pytest.mark.asyncio
async def test_run_budget_adapter_rejects_replayed_identity() -> None:
    context, _, profiles = setup_chain()
    sessions = FakeSessionFactory()
    ledger = FakeRunBudgetLedger(created=False)
    adapter = RunBudgetAttemptAdapter(
        sessions,
        workspace_id=profiles[0].workspace_id,
        run_id=uuid4(),
        call_identity="run-1:round-3",
        price_source=FakePriceSource(AttemptCostUpperBound(Decimal("1"), "CNY", "price-book:v1")),
        ledger=ledger,
    )

    with pytest.raises(ModelAttemptBudgetError) as raised:
        await adapter.reserve_attempt(
            context,
            profiles[0],
            request(),
            provider="test-provider",
            operation="generate",
            profile_index=0,
            attempt_index=0,
            attempt_kind=ModelAttemptKind.MAIN_MODEL,
        )

    assert raised.value.code == "RUN_BUDGET_ATTEMPT_REPLAYED"
    assert sessions.events[-2:] == ["rollback", "session.exit"]


@pytest.mark.asyncio
async def test_run_budget_adapter_requires_an_injected_price_bound() -> None:
    context, _, profiles = setup_chain()
    sessions = FakeSessionFactory()
    ledger = FakeRunBudgetLedger()
    adapter = RunBudgetAttemptAdapter(
        sessions,
        workspace_id=profiles[0].workspace_id,
        run_id=uuid4(),
        call_identity="run-1:round-3",
        price_source=FakePriceSource(),
        ledger=ledger,
    )

    with pytest.raises(ModelAttemptBudgetError) as raised:
        await adapter.reserve_attempt(
            context,
            profiles[0],
            request(),
            provider="test-provider",
            operation="generate",
            profile_index=0,
            attempt_index=0,
            attempt_kind=ModelAttemptKind.MAIN_MODEL,
        )

    assert raised.value.code == "RUN_BUDGET_UPPER_BOUND_UNAVAILABLE"
    assert ledger.reserve_calls == []


@pytest.mark.asyncio
async def test_run_budget_adapter_marks_unknown_in_a_separate_commit() -> None:
    context, _, profiles = setup_chain()
    workspace_id = profiles[0].workspace_id
    run_id = uuid4()
    sessions = FakeSessionFactory()
    ledger = FakeRunBudgetLedger()
    adapter = RunBudgetAttemptAdapter(
        sessions,
        workspace_id=workspace_id,
        run_id=run_id,
        call_identity="run-1:round-3",
        price_source=FakePriceSource(AttemptCostUpperBound(Decimal("1"), "CNY", "price-book:v1")),
        ledger=ledger,
    )
    reservation = ModelAttemptBudgetReservation(
        workspace_id=workspace_id,
        run_id=run_id,
        attempt_identity="model:run-1:round-3:generate:0:0",
        attempt_kind=ModelAttemptKind.MAIN_MODEL,
        cost_upper_bound=AttemptCostUpperBound(
            Decimal("1"),
            "CNY",
            "price-book:v1",
        ),
    )

    await adapter.mark_unknown_outcome(reservation)

    assert ledger.unknown_calls == [
        {
            "session": ledger.unknown_calls[0]["session"],
            "workspace_id": workspace_id,
            "run_id": run_id,
            "attempt_identity": "model:run-1:round-3:generate:0:0",
        }
    ]
    assert sessions.events == [
        "session.enter",
        "begin",
        "commit",
        "session.exit",
    ]


@pytest.mark.asyncio
async def test_retry_and_fallback_reserve_each_provider_attempt_in_order() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    provider = RecordingProvider(events)
    provider.complete_outcomes = {
        "primary": [
            ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True),
            ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True),
        ],
        "fallback": [response("fallback")],
    }
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )

    result = await gateway.generate(context, profiles[0].id, request(retry_attempts=2))

    assert result.content == "fallback"
    assert events == [
        ("reserve", "MAIN_MODEL"),
        ("provider.complete", "primary"),
        ("unknown", "MAIN_MODEL"),
        ("reserve", "RETRY"),
        ("provider.complete", "primary"),
        ("unknown", "RETRY"),
        ("reserve", "FALLBACK"),
        ("provider.complete", "fallback"),
    ]
    assert [reservation.attempt_kind for reservation in budget.reservations] == [
        ModelAttemptKind.MAIN_MODEL,
        ModelAttemptKind.RETRY,
        ModelAttemptKind.FALLBACK,
    ]


@pytest.mark.asyncio
async def test_successful_response_keeps_the_reservation_unreconciled() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    provider = RecordingProvider(events)
    provider.complete_outcomes = {
        "primary": [response("primary")],
        "fallback": [],
    }
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )

    result = await gateway.generate(context, profiles[0].id, request())

    assert result.content == "primary"
    assert events == [
        ("reserve", "MAIN_MODEL"),
        ("provider.complete", "primary"),
    ]
    assert budget.unknown == []


@pytest.mark.asyncio
async def test_reservation_failure_stops_before_provider_dispatch() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    budget.failure_code = "RUN_BUDGET_INSUFFICIENT_FUNDS"
    provider = RecordingProvider(events)
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )

    with pytest.raises(ModelAttemptBudgetError) as raised:
        await gateway.generate(context, profiles[0].id, request())

    assert raised.value.code == "RUN_BUDGET_INSUFFICIENT_FUNDS"
    assert events == [("reserve", "MAIN_MODEL")]


@pytest.mark.asyncio
async def test_prepared_frozen_stream_keeps_attempt_budget_across_session_exit() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    provider = RecordingProvider(events)
    provider.stream_outcomes = {
        "primary": ["pre-visible-error"],
        "fallback": ["success"],
    }
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )
    plan = frozen_plan(profiles)
    prepared = await gateway.prepare_resolved(
        context,
        plan,
        request(),
        operation="stream",
    )

    streamed = [event async for event in prepared.stream_resolved(context, plan, request())]

    assert streamed[-1].event_type is ModelStreamEventType.COMPLETED
    assert events == [
        ("reserve", "MAIN_MODEL"),
        ("provider.stream", "primary"),
        ("unknown", "MAIN_MODEL"),
        ("reserve", "FALLBACK"),
        ("provider.stream", "fallback"),
    ]


@pytest.mark.asyncio
async def test_stream_pre_visible_failure_reserves_retry_path_and_falls_back() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    provider = RecordingProvider(events)
    provider.stream_outcomes = {
        "primary": ["pre-visible-error"],
        "fallback": ["success"],
    }
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )

    streamed = [event async for event in gateway.stream(context, profiles[0].id, request())]

    assert [event.event_type for event in streamed] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    assert events == [
        ("reserve", "MAIN_MODEL"),
        ("provider.stream", "primary"),
        ("unknown", "MAIN_MODEL"),
        ("reserve", "FALLBACK"),
        ("provider.stream", "fallback"),
    ]


@pytest.mark.asyncio
async def test_stream_post_visible_failure_retains_hold_and_never_falls_back() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    provider = RecordingProvider(events)
    provider.stream_outcomes = {
        "primary": ["post-visible-error"],
        "fallback": ["success"],
    }
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )
    received: list[ModelStreamEvent] = []

    with pytest.raises(ModelGatewayError) as raised:
        async for event in gateway.stream(
            context,
            profiles[0].id,
            request(retry_attempts=3),
        ):
            received.append(event)

    assert raised.value.code == ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
    assert [event.message_delta for event in received if event.message_delta] == ["partial"]
    assert events == [
        ("reserve", "MAIN_MODEL"),
        ("provider.stream", "primary"),
        ("unknown", "MAIN_MODEL"),
    ]
    assert len(budget.unknown) == 1


@pytest.mark.asyncio
async def test_stream_cancellation_marks_the_reserved_attempt_unknown() -> None:
    context, repository, profiles = setup_chain()
    events: list[tuple[str, str | None]] = []
    budget = RecordingAttemptBudget(events)
    entered = asyncio.Event()

    class BlockingProvider(RecordingProvider):
        async def stream(self, profile, credential, request) -> AsyncIterator[ModelStreamEvent]:
            del credential, request
            self.events.append(("provider.stream", profile.model))
            entered.set()
            await asyncio.Event().wait()
            yield

    provider = BlockingProvider(events)
    gateway = ModelGatewayService(
        repository,
        provider,
        attempt_budget=budget,
    )
    stream = gateway.stream(context, profiles[0].id, request())
    next_event = asyncio.create_task(anext(stream))
    await entered.wait()

    next_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_event

    assert events == [
        ("reserve", "MAIN_MODEL"),
        ("provider.stream", "primary"),
        ("unknown", "MAIN_MODEL"),
    ]
    assert len(budget.unknown) == 1


__all__ = ["RecordingAttemptBudget"]
