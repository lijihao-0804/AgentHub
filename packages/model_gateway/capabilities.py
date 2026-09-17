"""Capability conversion and strict validation for model profiles."""

from collections.abc import Mapping
from typing import Any

from packages.model_gateway.contracts import (
    CapabilityRequirements,
    ModelCapabilities,
    ModelRequest,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode

_BOOLEAN_CAPABILITIES = ("tool_calling", "streaming", "structured_output", "vision")


def capabilities_from_mapping(values: Mapping[str, Any]) -> ModelCapabilities:
    """Convert JSON profile capabilities to the stable internal type."""

    return ModelCapabilities(
        tool_calling=values.get("tool_calling") is True,
        streaming=values.get("streaming") is True,
        structured_output=values.get("structured_output") is True,
        vision=values.get("vision") is True,
        max_context_tokens=(
            values.get("max_context_tokens")
            if isinstance(values.get("max_context_tokens"), int)
            and not isinstance(values.get("max_context_tokens"), bool)
            else None
        ),
    )


def validate_capabilities(
    values: ModelCapabilities | Mapping[str, Any],
    required: CapabilityRequirements,
) -> ModelCapabilities:
    """Require every declared capability, including numeric context capacity."""

    capabilities = (
        values if isinstance(values, ModelCapabilities) else capabilities_from_mapping(values)
    )
    for name in required.required:
        if name not in _BOOLEAN_CAPABILITIES or not getattr(capabilities, name):
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH)
    if required.max_context_tokens is not None:
        available = capabilities.max_context_tokens
        if available is None or available < required.max_context_tokens:
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH)
    return capabilities


def effective_capability_requirements(
    request: ModelRequest,
    mode: str,
) -> CapabilityRequirements:
    """Combine caller requirements with requirements implied by the gateway mode."""

    required = set(request.required_capabilities.required)
    if request.tools:
        required.add("tool_calling")
    if request.response_schema is not None:
        required.add("structured_output")
    if mode == "stream":
        required.add("streaming")
    elif mode != "generate":
        raise ValueError("unsupported model gateway mode")
    return CapabilityRequirements(
        required=frozenset(required),
        max_context_tokens=request.required_capabilities.max_context_tokens,
    )


__all__ = [
    "capabilities_from_mapping",
    "effective_capability_requirements",
    "validate_capabilities",
]
