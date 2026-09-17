"""Gateway orchestration for capability checks, retries, and fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelGateway,
    ModelHealthResult,
    ModelHealthStatus,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ProviderCredential
from packages.model_gateway.profile_resolution import (
    ModelProfileResolver,
    ResolvedModelProfile,
)
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository


class ModelProviderAdapter(Protocol):
    async def complete(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> ModelResponse: ...

    def stream(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]: ...

    async def health(
        self,
        profile: ResolvedModelProfile,
        credential: ProviderCredential,
    ) -> None: ...


class ModelGatewayService(ModelGateway):
    """Application-facing gateway with one owner for retry/fallback semantics."""

    def __init__(
        self,
        repository: SqlAlchemyModelGatewayRepository,
        adapter: ModelProviderAdapter,
    ) -> None:
        self.repository = repository
        self.resolver = ModelProfileResolver(repository)
        self.adapter = adapter

    async def generate(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> ModelResponse:
        chain = await self.resolver.resolve_chain(
            context,
            model_profile_id,
            required_capabilities=request.required_capabilities,
        )
        last_error: ModelGatewayError | None = None
        for profile in chain:
            credential = await self._enabled_credential(context, profile.provider_credential_id)
            for attempt in range(request.retry_policy.max_attempts):
                try:
                    return await self.adapter.complete(profile, credential, request)
                except ModelGatewayError as error:
                    last_error = error
                    if not error.retryable or attempt + 1 >= request.retry_policy.max_attempts:
                        break
        if last_error is not None:
            raise last_error
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE)

    def stream(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(context, model_profile_id, request)

    async def _stream(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        chain = await self.resolver.resolve_chain(
            context,
            model_profile_id,
            required_capabilities=request.required_capabilities,
        )
        last_error: ModelGatewayError | None = None
        for profile in chain:
            credential = await self._enabled_credential(context, profile.provider_credential_id)
            for attempt in range(request.retry_policy.max_attempts):
                buffered: list[ModelStreamEvent] = []
                visible = False
                try:
                    async for event in self.adapter.stream(profile, credential, request):
                        if event.event_type in {
                            ModelStreamEventType.MESSAGE_DELTA,
                            ModelStreamEventType.TOOL_CALL_DELTA,
                        }:
                            visible = True
                            for buffered_event in buffered:
                                yield buffered_event
                            buffered.clear()
                            yield event
                        elif visible:
                            yield event
                        else:
                            buffered.append(event)
                    for buffered_event in buffered:
                        yield buffered_event
                    return
                except ModelGatewayError as error:
                    if visible:
                        raise ModelGatewayError(
                            ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
                        ) from error
                    last_error = error
                    if not error.retryable or attempt + 1 >= request.retry_policy.max_attempts:
                        break
        if last_error is not None:
            raise last_error
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE)

    async def health(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
    ) -> ModelHealthResult:
        try:
            chain = await self.resolver.resolve_chain(context, model_profile_id)
            profile = chain[0]
            credential = await self._enabled_credential(context, profile.provider_credential_id)
            await self.adapter.health(profile, credential)
        except ModelGatewayError as error:
            status = (
                ModelHealthStatus.DEGRADED
                if error.code == ModelGatewayErrorCode.MODEL_RATE_LIMITED
                else ModelHealthStatus.UNAVAILABLE
            )
            return ModelHealthResult(status=status, failure_code=error.code.value)
        return ModelHealthResult(status=ModelHealthStatus.HEALTHY)

    async def capabilities(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
    ) -> ModelCapabilities:
        chain = await self.resolver.resolve_chain(context, model_profile_id)
        return chain[0].capabilities

    async def _enabled_credential(
        self,
        context: WorkspaceExecutionContext,
        credential_id: UUID,
    ) -> ProviderCredential:
        credential = await self.repository.get_provider_credential(context, credential_id)
        if credential is None or credential.enabled is False:
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROFILE_DISABLED)
        return credential

class SqlAlchemyModelGateway(ModelGatewayService):
    def __init__(
        self,
        session: AsyncSession,
        adapter: ModelProviderAdapter | None = None,
    ) -> None:
        super().__init__(
            SqlAlchemyModelGatewayRepository(session),
            adapter or LiteLLMProviderAdapter(),
        )


__all__ = ["ModelGatewayService", "ModelProviderAdapter", "SqlAlchemyModelGateway"]
