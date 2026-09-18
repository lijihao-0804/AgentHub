from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

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
    ModelToolCallDelta,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.models import ModelProfile, ProviderCredential


class FrozenStreamRepository:
    def __init__(
        self,
        profiles: list[ModelProfile],
        credentials: list[ProviderCredential],
    ) -> None:
        self.profiles = {profile.id: profile for profile in profiles}
        self.credentials = {credential.id: credential for credential in credentials}

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
        credential = self.credentials.get(credential_id)
        return (
            credential
            if credential and str(credential.workspace_id) == context.workspace_id
            else None
        )


class DeterministicStreamAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.outcomes: dict[str, list[str]] = {}

    async def stream(self, profile, credential, request) -> AsyncIterator[ModelStreamEvent]:
        del request
        self.calls.append(
            {
                "model": profile.model,
                "temperature": profile.temperature,
                "max_tokens": profile.max_tokens,
                "timeout_seconds": profile.timeout_seconds,
                "capabilities": profile.capabilities,
                "provider": credential.provider,
                "secret": credential.secret,
            }
        )
        outcome = self.outcomes[profile.model].pop(0)
        if outcome == "pre-visible-error":
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        if outcome == "message-interrupted":
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta="partial message",
            )
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)
        if outcome == "tool-interrupted":
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call_delta=ModelToolCallDelta(index=0, name="lookup"),
            )
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)
        if outcome != "success":
            raise AssertionError(f"unsupported deterministic outcome: {outcome}")
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta="complete message",
        )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=ModelResponse(
                content="complete message",
                provider=credential.provider,
                model=profile.model,
            ),
        )


def context_for(workspace_id: UUID) -> WorkspaceExecutionContext:
    principal = PrincipalContext(request_id="m4d-stream", trace_id="m4d-stream")
    organization = OrganizationContext(
        principal=principal,
        organization_id=str(uuid4()),
        org_role="OWNER",
    )
    return WorkspaceExecutionContext(
        organization=organization,
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
    )


def model_request(*, retry_attempts: int = 1) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        retry_policy=RetryPolicy(max_attempts=retry_attempts),
    )


def setup_profiles() -> tuple[
    WorkspaceExecutionContext,
    FrozenStreamRepository,
    ModelProfile,
    ModelProfile,
    ModelProfile,
    ProviderCredential,
]:
    workspace_id = uuid4()
    credential = ProviderCredential(
        id=uuid4(),
        workspace_id=workspace_id,
        provider="provider-a",
        name="rotatable",
        secret="original-secret",
    )
    fallback = ModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model="db-fallback-b",
        temperature=Decimal("0.1"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities={"streaming": True, "max_context_tokens": 8192},
    )
    drifted_fallback = ModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model="db-fallback-c",
        temperature=Decimal("0.1"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities={"streaming": True, "max_context_tokens": 8192},
    )
    primary = ModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model="db-primary-a",
        temperature=Decimal("0.1"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        fallback_profile_id=fallback.id,
        capabilities={"streaming": True, "max_context_tokens": 8192},
    )
    repository = FrozenStreamRepository([primary, fallback, drifted_fallback], [credential])
    return context_for(workspace_id), repository, primary, fallback, drifted_fallback, credential


def frozen_profile(
    profile: ModelProfile,
    *,
    provider: str = "provider-a",
    model: str,
    temperature: str = "0.25",
    max_tokens: int = 321,
    timeout_seconds: str = "7",
) -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=profile.id,
        workspace_id=profile.workspace_id,
        provider_credential_id=profile.provider_credential_id,
        provider=provider,
        model=model,
        temperature=Decimal(temperature),
        max_tokens=max_tokens,
        timeout_seconds=Decimal(timeout_seconds),
        capabilities=ModelCapabilities(streaming=True, max_context_tokens=16384),
    )


@pytest.mark.asyncio
async def test_frozen_stream_uses_snapshot_fields_rotated_secret_and_completed_response() -> None:
    context, repository, primary, fallback, _, credential = setup_profiles()
    plan = ResolvedModelExecutionPlan(
        primary=frozen_profile(primary, model="frozen-primary"),
        fallbacks=(
            frozen_profile(
                fallback,
                model="frozen-fallback",
                temperature="0.4",
                max_tokens=654,
                timeout_seconds="8",
            ),
        ),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    primary.model = "db-drifted-primary"
    primary.temperature = Decimal("0.001")
    primary.max_tokens = 1
    primary.timeout_seconds = Decimal("0.001")
    primary.capabilities = {"streaming": False}
    primary.fallback_profile_id = primary.id
    credential.secret = "rotated-secret"
    adapter = DeterministicStreamAdapter()
    adapter.outcomes = {"frozen-primary": ["success"], "frozen-fallback": []}
    gateway = ModelGatewayService(repository, adapter)

    events = [
        event
        async for event in gateway.stream_resolved(context, plan, model_request(retry_attempts=5))
    ]

    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    assert events[-1].response == ModelResponse(
        content="complete message",
        provider="provider-a",
        model="frozen-primary",
    )
    assert adapter.calls == [
        {
            "model": "frozen-primary",
            "temperature": Decimal("0.25"),
            "max_tokens": 321,
            "timeout_seconds": Decimal("7"),
            "capabilities": ModelCapabilities(streaming=True, max_context_tokens=16384),
            "provider": "provider-a",
            "secret": "rotated-secret",
        }
    ]


@pytest.mark.asyncio
async def test_frozen_stream_rejects_credential_provider_mismatch() -> None:
    context, repository, primary, _, _, credential = setup_profiles()
    plan = ResolvedModelExecutionPlan(
        primary=frozen_profile(primary, provider="provider-a", model="frozen-primary")
    )
    credential.provider = "provider-b"
    adapter = DeterministicStreamAdapter()
    gateway = ModelGatewayService(repository, adapter)

    with pytest.raises(ModelGatewayError) as raised:
        async for _ in gateway.stream_resolved(context, plan, model_request()):
            pass

    assert raised.value.code == ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_frozen_retry_policy_allows_pre_visible_retry() -> None:
    context, repository, primary, _, _, _ = setup_profiles()
    plan = ResolvedModelExecutionPlan(
        primary=frozen_profile(primary, model="frozen-primary"),
        retry_policy=RetryPolicy(max_attempts=2),
    )
    adapter = DeterministicStreamAdapter()
    adapter.outcomes = {"frozen-primary": ["pre-visible-error", "success"]}
    gateway = ModelGatewayService(repository, adapter)

    events = [
        event
        async for event in gateway.stream_resolved(context, plan, model_request(retry_attempts=1))
    ]

    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    assert [call["model"] for call in adapter.calls] == ["frozen-primary", "frozen-primary"]


@pytest.mark.asyncio
async def test_frozen_fallback_ignores_mutable_chain_drift() -> None:
    context, repository, primary, fallback, drifted_fallback, _ = setup_profiles()
    plan = ResolvedModelExecutionPlan(
        primary=frozen_profile(primary, model="frozen-primary"),
        fallbacks=(frozen_profile(fallback, model="frozen-fallback-b"),),
    )
    primary.fallback_profile_id = drifted_fallback.id
    fallback.model = "db-drifted-fallback"
    adapter = DeterministicStreamAdapter()
    adapter.outcomes = {
        "frozen-primary": ["pre-visible-error"],
        "frozen-fallback-b": ["success"],
    }
    gateway = ModelGatewayService(repository, adapter)

    events = [
        event
        async for event in gateway.stream_resolved(context, plan, model_request())
    ]

    assert events[-1].response is not None
    assert events[-1].response.model == "frozen-fallback-b"
    assert [call["model"] for call in adapter.calls] == [
        "frozen-primary",
        "frozen-fallback-b",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "event_type"),
    [
        ("message-interrupted", ModelStreamEventType.MESSAGE_DELTA),
        ("tool-interrupted", ModelStreamEventType.TOOL_CALL_DELTA),
    ],
)
async def test_frozen_post_visible_failure_interrupts_without_fallback(
    outcome: str,
    event_type: ModelStreamEventType,
) -> None:
    context, repository, primary, fallback, _, _ = setup_profiles()
    plan = ResolvedModelExecutionPlan(
        primary=frozen_profile(primary, model="frozen-primary"),
        fallbacks=(frozen_profile(fallback, model="frozen-fallback"),),
        retry_policy=RetryPolicy(max_attempts=3),
    )
    adapter = DeterministicStreamAdapter()
    adapter.outcomes = {"frozen-primary": [outcome], "frozen-fallback": ["success"]}
    gateway = ModelGatewayService(repository, adapter)
    received: list[ModelStreamEvent] = []

    with pytest.raises(ModelGatewayError) as raised:
        async for event in gateway.stream_resolved(context, plan, model_request()):
            received.append(event)

    assert raised.value.code == ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
    assert len(adapter.calls) == 1
    assert received[0].event_type == event_type


def test_frozen_plan_profile_has_immutable_value_semantics() -> None:
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider="provider-a",
        model="frozen",
        temperature=Decimal("0"),
        max_tokens=1,
        timeout_seconds=Decimal("1"),
        capabilities=ModelCapabilities(streaming=True),
    )

    with pytest.raises((AttributeError, TypeError)):
        profile.model = "mutable"


__all__ = ["FrozenStreamRepository", "DeterministicStreamAdapter"]
