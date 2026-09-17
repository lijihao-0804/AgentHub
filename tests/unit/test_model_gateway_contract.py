from decimal import Decimal
from typing import get_type_hints
from uuid import UUID

import pytest

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.contracts import (
    CapabilityRequirements,
    CostEstimate,
    ModelCapabilities,
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolCallDelta,
    ModelToolDefinition,
    ModelUsage,
    RetryPolicy,
    StructuredOutputSchema,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode


def test_model_request_is_provider_neutral_and_supports_future_requirements() -> None:
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        tools=(
            ModelToolDefinition(
                name="lookup",
                parameters={"type": "object"},
            ),
        ),
        response_schema=StructuredOutputSchema(
            name="answer",
            json_schema={"type": "object"},
        ),
        required_capabilities=CapabilityRequirements(
            required={"tool_calling", "structured_output"},
            max_context_tokens=4096,
        ),
        retry_policy=RetryPolicy(max_attempts=2),
    )

    assert request.messages[0].content == "hello"
    assert request.tools[0].name == "lookup"
    assert request.required_capabilities.required == {
        "tool_calling",
        "structured_output",
    }
    assert not {"model", "provider", "credential", "secret"}.intersection(
        ModelRequest.__dataclass_fields__
    )


def test_tool_result_message_requires_tool_call_id() -> None:
    message = ModelMessage(role="tool", content="{}", tool_call_id="call-1")

    assert message.tool_call_id == "call-1"
    with pytest.raises(ValueError):
        ModelMessage(role="tool", content="{}")
    with pytest.raises(ValueError):
        ModelMessage(role="user", content="hello", tool_call_id="call-1")


def test_model_response_stream_events_and_decimal_cost_are_typed() -> None:
    usage = ModelUsage(input_tokens=2, output_tokens=3, total_tokens=5, cached_tokens=1)
    response = ModelResponse(
        content="done",
        provider="deepseek",
        model="deepseek-chat",
        tool_calls=(ModelToolCall(name="lookup", arguments={"q": "x"}),),
        finish_reason="stop",
        usage=usage,
        cost_estimate=CostEstimate(amount=Decimal("0.0012"), currency="usd"),
    )
    events = (
        ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta="d",
        ),
        ModelStreamEvent(
            event_type=ModelStreamEventType.TOOL_CALL_DELTA,
            tool_call_delta=ModelToolCallDelta(index=0, name="lookup"),
        ),
        ModelStreamEvent(event_type=ModelStreamEventType.USAGE, usage=usage),
        ModelStreamEvent(event_type=ModelStreamEventType.COMPLETED, response=response),
    )

    assert events[-1].response == response
    assert response.cost_estimate is not None
    assert response.cost_estimate.amount == Decimal("0.0012")
    assert response.cost_estimate.currency == "USD"


def test_stream_event_rejects_conflicting_or_missing_payloads() -> None:
    usage = ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    response = ModelResponse(content="done", provider="deepseek", model="deepseek-chat")

    with pytest.raises(ValueError):
        ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta="d",
            usage=usage,
        )
    with pytest.raises(ValueError):
        ModelStreamEvent(event_type=ModelStreamEventType.COMPLETED)
    with pytest.raises(ValueError):
        ModelStreamEvent(
            event_type=ModelStreamEventType.USAGE,
            usage=usage,
            response=response,
        )


def test_gateway_entrypoints_require_context_and_profile_id() -> None:
    hints = get_type_hints(ModelGateway.generate)

    assert hints["context"] is WorkspaceExecutionContext
    assert hints["model_profile_id"] is UUID


def test_contracts_bound_invalid_limits_and_normalize_error_safely() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=6)
    with pytest.raises(ValueError):
        CapabilityRequirements(max_context_tokens=0)

    error = ModelGatewayError(
        ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
        retryable=True,
    )
    assert error.code == ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE
    assert error.retryable is True
    assert "secret" not in repr(error).lower()
    assert "raw" not in str(error).lower()


def test_capability_and_health_contracts_are_provider_neutral() -> None:
    capabilities = ModelCapabilities(streaming=True, max_context_tokens=8192)

    assert capabilities.streaming is True
    assert capabilities.max_context_tokens == 8192
