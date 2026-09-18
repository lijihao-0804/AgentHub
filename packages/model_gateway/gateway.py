"""Gateway orchestration for capability checks, retries, and fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.capabilities import (
    effective_capability_requirements,
    validate_capabilities,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelGateway,
    ModelHealthResult,
    ModelHealthStatus,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ProviderCredential
from packages.model_gateway.profile_resolution import (
    ModelProfileResolver,
    ResolvedModelProfile,
)
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository
from packages.observability.contracts import TraceSink, TraceSpan
from packages.observability.noop import NoopTraceSink, NoopTraceSpan

logger = logging.getLogger(__name__)


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
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.repository = repository
        self.resolver = ModelProfileResolver(repository)
        self.adapter = adapter
        self.trace_sink = trace_sink or NoopTraceSink()

    async def generate(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> ModelResponse:
        chain = await self.resolver.resolve_chain(
            context,
            model_profile_id,
            required_capabilities=effective_capability_requirements(request, "generate"),
        )
        return await self._generate_chain(context, model_profile_id, chain, request)

    async def generate_resolved(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> ModelResponse:
        """Execute the non-secret identities frozen in a published AgentVersion."""

        required = effective_capability_requirements(request, "generate")
        adapter_profiles: list[ResolvedModelProfile] = []
        credentials: dict[UUID, ProviderCredential] = {}
        try:
            workspace_id = UUID(context.workspace_id)
        except ValueError:
            raise ModelGatewayError(
                ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
            ) from None
        for frozen in plan.chain:
            if frozen.workspace_id != workspace_id:
                raise ModelGatewayError(ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID)
            try:
                validate_capabilities(frozen.capabilities, required)
                credential = await self._enabled_frozen_credential(context, frozen)
            except ModelGatewayError as error:
                if error.code is ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH:
                    raise
                raise ModelGatewayError(
                    ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
                ) from None
            adapter_profiles.append(
                ResolvedModelProfile(
                    id=frozen.id,
                    workspace_id=frozen.workspace_id,
                    provider_credential_id=frozen.provider_credential_id,
                    model=frozen.model,
                    temperature=frozen.temperature,
                    max_tokens=frozen.max_tokens,
                    timeout_seconds=frozen.timeout_seconds,
                    fallback_profile_id=None,
                    capabilities=frozen.capabilities,
                )
            )
            credentials[frozen.id] = credential
        return await self._generate_chain(
            context,
            plan.primary.id,
            tuple(adapter_profiles),
            replace(request, retry_policy=plan.retry_policy),
            credentials=credentials,
        )

    async def _generate_chain(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        chain: tuple[ResolvedModelProfile, ...],
        request: ModelRequest,
        *,
        credentials: Mapping[UUID, ProviderCredential] | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        span = await _start_trace_span(
            self.trace_sink,
            "model.generate",
            _initial_trace_attributes(context, model_profile_id),
        )
        attempt_count = 0
        fallback_used = False
        provider: str | None = None
        model: str | None = None
        try:
            last_error: ModelGatewayError | None = None
            for profile_index, profile in enumerate(chain):
                fallback_used = profile_index > 0
                model = profile.model
                credential = (
                    credentials[profile.id]
                    if credentials is not None
                    else await self._enabled_credential(context, profile.provider_credential_id)
                )
                provider = credential.provider
                for attempt in range(request.retry_policy.max_attempts):
                    attempt_count += 1
                    try:
                        async with asyncio.timeout(float(profile.timeout_seconds)):
                            response = await self.adapter.complete(profile, credential, request)
                        await _end_trace_span(
                            span,
                            attributes=_response_trace_attributes(
                                context,
                                model_profile_id,
                                started=started,
                                attempt_count=attempt_count,
                                fallback_used=fallback_used,
                                response=response,
                            ),
                        )
                        return response
                    except TimeoutError:
                        last_error = ModelGatewayError(
                            ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True
                        )
                        if attempt + 1 >= request.retry_policy.max_attempts:
                            break
                    except ModelGatewayError as error:
                        last_error = error
                        if not error.retryable or attempt + 1 >= request.retry_policy.max_attempts:
                            break
            if last_error is not None:
                raise last_error
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE)
        except ModelGatewayError as error:
            await _end_trace_span(
                span,
                attributes=_failure_trace_attributes(
                    context,
                    model_profile_id,
                    started=started,
                    attempt_count=attempt_count,
                    fallback_used=fallback_used,
                    provider=provider,
                    model=model,
                ),
                status="error",
                failure_code=error.code.value,
            )
            raise
        except Exception:
            normalized = ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
            await _end_trace_span(
                span,
                attributes=_failure_trace_attributes(
                    context,
                    model_profile_id,
                    started=started,
                    attempt_count=attempt_count,
                    fallback_used=fallback_used,
                    provider=provider,
                    model=model,
                ),
                status="error",
                failure_code=normalized.code.value,
            )
            raise normalized from None

    def stream(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        return self._stream(context, model_profile_id, request)

    def stream_resolved(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Stream using the immutable model chain from a published version."""

        return self._stream(
            context,
            plan.primary.id,
            replace(request, retry_policy=plan.retry_policy),
            frozen_plan=plan,
        )

    async def _prepare_frozen_stream(
        self,
        context: WorkspaceExecutionContext,
        plan: ResolvedModelExecutionPlan,
        request: ModelRequest,
    ) -> tuple[tuple[ResolvedModelProfile, ...], dict[UUID, ProviderCredential]]:
        required = effective_capability_requirements(request, "stream")
        try:
            workspace_id = UUID(context.workspace_id)
        except (TypeError, ValueError):
            raise ModelGatewayError(
                ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
            ) from None

        adapter_profiles: list[ResolvedModelProfile] = []
        credentials: dict[UUID, ProviderCredential] = {}
        for frozen in plan.chain:
            if frozen.workspace_id != workspace_id:
                raise ModelGatewayError(ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID)
            try:
                validate_capabilities(frozen.capabilities, required)
                credential = await self._enabled_frozen_credential(context, frozen)
            except ModelGatewayError as error:
                if error.code is ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH:
                    raise
                raise ModelGatewayError(
                    ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
                ) from None
            adapter_profiles.append(
                ResolvedModelProfile(
                    id=frozen.id,
                    workspace_id=frozen.workspace_id,
                    provider_credential_id=frozen.provider_credential_id,
                    model=frozen.model,
                    temperature=frozen.temperature,
                    max_tokens=frozen.max_tokens,
                    timeout_seconds=frozen.timeout_seconds,
                    fallback_profile_id=None,
                    capabilities=frozen.capabilities,
                )
            )
            credentials[frozen.id] = credential
        return tuple(adapter_profiles), credentials

    async def _stream(
        self,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
        request: ModelRequest,
        *,
        frozen_plan: ResolvedModelExecutionPlan | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        started = time.perf_counter()
        span = await _start_trace_span(
            self.trace_sink,
            "model.generate",
            _initial_trace_attributes(context, model_profile_id),
        )
        attempt_count = 0
        fallback_used = False
        provider: str | None = None
        model: str | None = None
        try:
            if frozen_plan is None:
                chain = await self.resolver.resolve_chain(
                    context,
                    model_profile_id,
                    required_capabilities=effective_capability_requirements(request, "stream"),
                )
                credentials: Mapping[UUID, ProviderCredential] | None = None
            else:
                chain, credentials = await self._prepare_frozen_stream(
                    context, frozen_plan, request
                )
            last_error: ModelGatewayError | None = None
            for profile_index, profile in enumerate(chain):
                fallback_used = profile_index > 0
                model = profile.model
                credential = (
                    credentials[profile.id]
                    if credentials is not None
                    else await self._enabled_credential(context, profile.provider_credential_id)
                )
                provider = credential.provider
                for attempt in range(request.retry_policy.max_attempts):
                    attempt_count += 1
                    buffered: list[ModelStreamEvent] = []
                    visible = False
                    final_response: ModelResponse | None = None
                    try:
                        async with asyncio.timeout(float(profile.timeout_seconds)):
                            async for event in self.adapter.stream(profile, credential, request):
                                if event.response is not None:
                                    final_response = event.response
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
                    except TimeoutError:
                        if visible:
                            raise ModelGatewayError(
                                ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
                            ) from None
                        last_error = ModelGatewayError(
                            ModelGatewayErrorCode.MODEL_TIMEOUT,
                            retryable=True,
                        )
                        if attempt + 1 >= request.retry_policy.max_attempts:
                            break
                        continue
                    except ModelGatewayError as error:
                        if visible:
                            raise ModelGatewayError(
                                ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
                            ) from None
                        last_error = error
                        if not error.retryable or attempt + 1 >= request.retry_policy.max_attempts:
                            break
                        continue
                    except Exception:
                        if visible:
                            raise ModelGatewayError(
                                ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
                            ) from None
                        last_error = ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
                        break
                    for buffered_event in buffered:
                        yield buffered_event
                    await _end_trace_span(
                        span,
                        attributes=_response_trace_attributes(
                            context,
                            model_profile_id,
                            started=started,
                            attempt_count=attempt_count,
                            fallback_used=fallback_used,
                            response=final_response,
                            provider=provider,
                            model=model,
                        ),
                    )
                    return
            if last_error is not None:
                raise last_error
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE)
        except ModelGatewayError as error:
            await _end_trace_span(
                span,
                attributes=_failure_trace_attributes(
                    context,
                    model_profile_id,
                    started=started,
                    attempt_count=attempt_count,
                    fallback_used=fallback_used,
                    provider=provider,
                    model=model,
                ),
                status="error",
                failure_code=error.code.value,
            )
            raise
        except Exception:
            normalized = ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
            await _end_trace_span(
                span,
                attributes=_failure_trace_attributes(
                    context,
                    model_profile_id,
                    started=started,
                    attempt_count=attempt_count,
                    fallback_used=fallback_used,
                    provider=provider,
                    model=model,
                ),
                status="error",
                failure_code=normalized.code.value,
            )
            raise normalized from None

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

    async def _enabled_frozen_credential(
        self,
        context: WorkspaceExecutionContext,
        profile: ResolvedModelExecutionProfile,
    ) -> ProviderCredential:
        credential = await self.repository.get_provider_credential(
            context, profile.provider_credential_id
        )
        if (
            credential is None
            or credential.enabled is False
            or credential.workspace_id != profile.workspace_id
            or credential.provider != profile.provider
        ):
            raise ModelGatewayError(ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID)
        return credential


class SqlAlchemyModelGateway(ModelGatewayService):
    def __init__(
        self,
        session: AsyncSession,
        adapter: ModelProviderAdapter | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        super().__init__(
            SqlAlchemyModelGatewayRepository(session),
            adapter or LiteLLMProviderAdapter(),
            trace_sink=trace_sink,
        )


def _initial_trace_attributes(
    context: WorkspaceExecutionContext,
    model_profile_id: UUID,
) -> dict[str, Any]:
    return {
        "request_id": context.request_id,
        "trace_id": context.organization.principal.trace_id,
        "workspace_id": context.workspace_id,
        "profile_id": str(model_profile_id),
    }


def _response_trace_attributes(
    context: WorkspaceExecutionContext,
    model_profile_id: UUID,
    *,
    started: float,
    attempt_count: int,
    fallback_used: bool,
    response: ModelResponse | None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    attributes = _initial_trace_attributes(context, model_profile_id)
    attributes.update(
        {
            "provider": response.provider if response is not None else provider,
            "model": response.model if response is not None else model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "attempt_count": attempt_count,
            "fallback_used": fallback_used,
        }
    )
    if response is not None and response.usage is not None:
        attributes.update(
            {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
                "cached_tokens": response.usage.cached_tokens,
            }
        )
    if response is not None and response.cost_estimate is not None:
        attributes.update(
            {
                "estimated_cost": response.cost_estimate.amount,
                "currency": response.cost_estimate.currency,
            }
        )
    return attributes


def _failure_trace_attributes(
    context: WorkspaceExecutionContext,
    model_profile_id: UUID,
    *,
    started: float,
    attempt_count: int,
    fallback_used: bool,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    return _response_trace_attributes(
        context,
        model_profile_id,
        started=started,
        attempt_count=attempt_count,
        fallback_used=fallback_used,
        response=None,
        provider=provider,
        model=model,
    )


async def _start_trace_span(
    trace_sink: TraceSink,
    name: str,
    attributes: Mapping[str, Any],
) -> TraceSpan:
    try:
        return await trace_sink.start_span(name, attributes=attributes)
    except Exception as error:
        logger.warning("trace start failed: %s", type(error).__name__)
        return NoopTraceSpan()


async def _end_trace_span(
    span: TraceSpan,
    *,
    attributes: Mapping[str, Any],
    status: str = "ok",
    failure_code: str | None = None,
) -> None:
    try:
        await span.end(attributes=attributes, status=status, failure_code=failure_code)
    except Exception as error:
        logger.warning("trace end failed: %s", type(error).__name__)


__all__ = ["ModelGatewayService", "ModelProviderAdapter", "SqlAlchemyModelGateway"]
