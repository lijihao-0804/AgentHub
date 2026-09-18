"""Safe, workspace-scoped model profile and fallback-chain resolution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.capabilities import validate_capabilities
from packages.model_gateway.contracts import CapabilityRequirements, ModelCapabilities
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile
from packages.model_gateway.repositories import SqlAlchemyModelGatewayRepository


@dataclass(frozen=True, slots=True)
class ResolvedModelProfile:
    """Profile data safe to pass between gateway layers; it contains no secret."""

    id: UUID
    workspace_id: UUID
    provider_credential_id: UUID
    model: str
    temperature: Decimal
    max_tokens: int
    timeout_seconds: Decimal
    fallback_profile_id: UUID | None
    capabilities: ModelCapabilities


def _to_resolved(profile: ModelProfile) -> ResolvedModelProfile:
    try:
        if profile.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if profile.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if profile.temperature < 0:
            raise ValueError("temperature must be non-negative")
        capabilities = validate_capabilities(profile.capabilities, CapabilityRequirements())
    except ModelGatewayError:
        raise
    except (ArithmeticError, TypeError, ValueError):
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE) from None
    return ResolvedModelProfile(
        id=profile.id,
        workspace_id=profile.workspace_id,
        provider_credential_id=profile.provider_credential_id,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
        fallback_profile_id=profile.fallback_profile_id,
        capabilities=capabilities,
    )


class ModelProfileResolver:
    def __init__(self, repository: SqlAlchemyModelGatewayRepository) -> None:
        self.repository = repository

    async def resolve_chain(
        self,
        context: WorkspaceExecutionContext,
        profile_id: UUID,
        *,
        required_capabilities: CapabilityRequirements | None = None,
    ) -> tuple[ResolvedModelProfile, ...]:
        required = required_capabilities or CapabilityRequirements()
        chain: list[ResolvedModelProfile] = []
        visited: set[UUID] = set()
        current_id: UUID | None = profile_id

        while current_id is not None:
            if current_id in visited:
                raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
            visited.add(current_id)
            profile = await self.repository.get_model_profile(context, current_id)
            if profile is None or profile.enabled is False:
                raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROFILE_DISABLED)
            resolved = _to_resolved(profile)
            validate_capabilities(resolved.capabilities, required)
            chain.append(resolved)
            current_id = resolved.fallback_profile_id

        return tuple(chain)


__all__ = ["ModelProfileResolver", "ResolvedModelProfile"]
