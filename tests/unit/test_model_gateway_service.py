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
    CapabilityRequirements,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCallDelta,
    ModelUsage,
    RetryPolicy,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.models import ModelProfile, ProviderCredential


class FakeRepository:
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

    async def list_model_profiles(self, context: WorkspaceExecutionContext):
        return [
            profile
            for profile in self.profiles.values()
            if str(profile.workspace_id) == context.workspace_id
        ]


class FakeAdapter:
    def __init__(self) -> None:
        self.complete_calls: list[str] = []
        self.stream_calls: list[str] = []
        self.complete_outcomes: dict[str, list[ModelResponse | ModelGatewayError]] = {}
        self.health_outcomes: dict[str, ModelGatewayError] = {}
        self.stream_outcomes: dict[str, str] = {}

    async def complete(self, profile, credential, request) -> ModelResponse:
        del credential, request
        self.complete_calls.append(profile.model)
        outcome = self.complete_outcomes[profile.model].pop(0)
        if isinstance(outcome, ModelGatewayError):
            raise outcome
        return outcome

    async def health(self, profile, credential) -> None:
        del credential
        error = self.health_outcomes.get(profile.model)
        if error is not None:
            raise error

    async def stream(self, profile, credential, request) -> AsyncIterator[ModelStreamEvent]:
        del credential, request
        self.stream_calls.append(profile.model)
        mode = self.stream_outcomes[profile.model]
        if mode == "pre-visible-error":
            raise ModelGatewayError(
                ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
                retryable=True,
            )
        if mode == "message-interrupted":
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA,
                message_delta="partial",
            )
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)
        if mode == "tool-interrupted":
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_call_delta=ModelToolCallDelta(index=0, name="lookup"),
            )
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta="fallback",
        )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=ModelResponse(
                content="fallback",
                provider="deepseek",
                model=profile.model,
            ),
        )


def context_for(workspace_id: UUID) -> WorkspaceExecutionContext:
    principal = PrincipalContext(request_id="m2-gateway", trace_id="m2-gateway")
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


def setup_chain(*, fallback_capabilities: dict | None = None) -> tuple[
    WorkspaceExecutionContext,
    list[ModelProfile],
    ProviderCredential,
]:
    workspace_id = uuid4()
    credential = ProviderCredential(
        id=uuid4(),
        workspace_id=workspace_id,
        provider="deepseek",
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
        capabilities=fallback_capabilities or {"streaming": True, "max_context_tokens": 8192},
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
    return context_for(workspace_id), [primary, fallback], credential


def response(model: str) -> ModelResponse:
    return ModelResponse(
        content=model,
        provider="deepseek",
        model=model,
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def request(*, retry_attempts: int = 1, required: CapabilityRequirements | None = None):
    return ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        retry_policy=RetryPolicy(max_attempts=retry_attempts),
        required_capabilities=required or CapabilityRequirements(),
    )


@pytest.mark.asyncio
async def test_retryable_primary_is_retried_once_then_falls_back() -> None:
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.complete_outcomes = {
        "primary": [
            ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True),
            ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True),
        ],
        "fallback": [response("fallback")],
    }
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)

    result = await gateway.generate(context, profiles[0].id, request(retry_attempts=2))

    assert result.content == "fallback"
    assert adapter.complete_calls == ["primary", "primary", "fallback"]


@pytest.mark.asyncio
async def test_non_retryable_primary_error_does_not_retry_before_fallback() -> None:
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.complete_outcomes = {
        "primary": [ModelGatewayError(ModelGatewayErrorCode.MODEL_AUTH_FAILED)],
        "fallback": [response("fallback")],
    }
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)

    result = await gateway.generate(context, profiles[0].id, request(retry_attempts=3))

    assert result.content == "fallback"
    assert adapter.complete_calls == ["primary", "fallback"]


@pytest.mark.asyncio
async def test_capability_mismatch_is_validated_across_full_fallback_chain() -> None:
    context, profiles, credential = setup_chain(fallback_capabilities={"streaming": False})
    adapter = FakeAdapter()
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate(
            context,
            profiles[0].id,
            request(required=CapabilityRequirements(required={"streaming"})),
        )

    assert raised.value.code == ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH
    assert adapter.complete_calls == []


@pytest.mark.asyncio
async def test_stream_error_before_first_visible_delta_can_fallback() -> None:
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.stream_outcomes = {"primary": "pre-visible-error", "fallback": "success"}
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)

    events = [
        event
        async for event in gateway.stream(
            context, profiles[0].id, request(retry_attempts=1)
        )
    ]

    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    assert adapter.stream_calls == ["primary", "fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["message-interrupted", "tool-interrupted"])
async def test_stream_error_after_visible_delta_never_falls_back(mode: str) -> None:
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.stream_outcomes = {"primary": mode, "fallback": "success"}
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)
    received: list[ModelStreamEvent] = []

    with pytest.raises(ModelGatewayError) as raised:
        async for event in gateway.stream(context, profiles[0].id, request(retry_attempts=3)):
            received.append(event)

    assert raised.value.code == ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
    assert len(adapter.stream_calls) == 1
    assert received[0].event_type in {
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.TOOL_CALL_DELTA,
    }


@pytest.mark.asyncio
async def test_disabled_profile_is_never_called() -> None:
    context, profiles, credential = setup_chain()
    profiles[0].enabled = False
    adapter = FakeAdapter()
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate(context, profiles[0].id, request())

    assert raised.value.code == ModelGatewayErrorCode.MODEL_PROFILE_DISABLED
    assert adapter.complete_calls == []


@pytest.mark.asyncio
async def test_health_failure_is_safe_and_normalized() -> None:
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.health_outcomes["primary"] = ModelGatewayError(
        ModelGatewayErrorCode.MODEL_TIMEOUT,
        retryable=True,
    )
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)

    result = await gateway.health(context, profiles[0].id)

    assert result.status.value == "unavailable"
    assert result.failure_code == "MODEL_TIMEOUT"
