"""Single-attempt LiteLLM adapter with AgentHub-native normalization."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from typing import Any, Protocol

from packages.model_gateway.contracts import (
    CostEstimate,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolCallDelta,
    ModelUsage,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile, ProviderCredential


class LiteLLMClient(Protocol):
    def acompletion(self, **kwargs: Any) -> Any: ...


def _default_client() -> LiteLLMClient:
    import litellm

    return litellm


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _provider_model(credential: ProviderCredential, model: str) -> str:
    provider = credential.provider.strip().lower().replace("_", "-")
    if provider == "deepseek":
        return model if model.startswith("deepseek/") else f"deepseek/{model}"
    if provider in {"openai-compatible", "openai"}:
        return model if model.startswith("openai/") else f"openai/{model}"
    raise ModelGatewayError(ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE)


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name is not None:
        payload["name"] = message.name
    if message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                **(
                    {"id": call.provider_tool_call_id}
                    if call.provider_tool_call_id is not None
                    else {}
                ),
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":")),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _request_payload(
    profile: ModelProfile,
    credential: ProviderCredential,
    request: ModelRequest,
    *,
    stream: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": _provider_model(credential, profile.model),
        "messages": [_message_payload(message) for message in request.messages],
        "temperature": float(profile.temperature),
        "max_tokens": profile.max_tokens,
        "timeout": float(profile.timeout_seconds),
        "api_key": credential.secret,
        "stream": stream,
    }
    if credential.base_url:
        payload["api_base"] = credential.base_url
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in request.tools
        ]
    if request.response_schema is not None:
        schema = request.response_schema
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.name,
                "schema": dict(schema.json_schema),
                "strict": schema.strict,
            },
        }
    return payload


def _normalize_error(error: BaseException) -> ModelGatewayError:
    error_name = type(error).__name__.lower()
    status_code = _value(error, "status_code")
    if "timeout" in error_name:
        return ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT, retryable=True)
    if status_code == 429 or "ratelimit" in error_name or "rate_limit" in error_name:
        return ModelGatewayError(ModelGatewayErrorCode.MODEL_RATE_LIMITED, retryable=True)
    if status_code in {401, 403} or "auth" in error_name:
        return ModelGatewayError(ModelGatewayErrorCode.MODEL_AUTH_FAILED)
    if status_code is not None and int(status_code) >= 500:
        return ModelGatewayError(
            ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
            retryable=True,
        )
    if any(marker in error_name for marker in ("connection", "unavailable", "service")):
        return ModelGatewayError(
            ModelGatewayErrorCode.MODEL_PROVIDER_UNAVAILABLE,
            retryable=True,
        )
    return ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)


def _normalize_usage(value: Any) -> ModelUsage | None:
    if value is None:
        return None
    input_tokens = _value(value, "prompt_tokens")
    output_tokens = _value(value, "completion_tokens")
    total_tokens = _value(value, "total_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    input_tokens = int(input_tokens)
    output_tokens = int(output_tokens)
    total_tokens = int(total_tokens or input_tokens + output_tokens)
    details = _value(value, "prompt_tokens_details")
    cached_tokens = _value(details, "cached_tokens") if details is not None else None
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=int(cached_tokens) if cached_tokens is not None else None,
    )


def _normalize_cost(response: Any) -> CostEstimate | None:
    cost = _value(response, "response_cost")
    if cost is None:
        hidden = _value(response, "_hidden_params", {})
        cost = _value(hidden, "response_cost")
    if cost is None:
        return None
    try:
        return CostEstimate(amount=Decimal(str(cost)), currency="USD")
    except (ArithmeticError, ValueError):
        return None


def _normalize_tool_call(tool_call: Any) -> ModelToolCall:
    function = _value(tool_call, "function", {})
    arguments = _value(function, "arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE) from error
    if not isinstance(arguments, Mapping):
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
    return ModelToolCall(
        name=str(_value(function, "name", "")),
        arguments=dict(arguments),
        provider_tool_call_id=_value(tool_call, "id"),
    )


def _normalize_response(
    response: Any,
    *,
    provider: str,
    model: str,
) -> ModelResponse:
    choices = _value(response, "choices", [])
    if not choices:
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
    choice = choices[0]
    message = _value(choice, "message", {})
    tool_calls = tuple(
        _normalize_tool_call(tool_call) for tool_call in (_value(message, "tool_calls", []) or [])
    )
    return ModelResponse(
        content=_value(message, "content", "") or "",
        provider=provider,
        model=model,
        tool_calls=tool_calls,
        finish_reason=_value(choice, "finish_reason"),
        usage=_normalize_usage(_value(response, "usage")),
        cost_estimate=_normalize_cost(response),
    )


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class LiteLLMProviderAdapter:
    """One provider attempt; retry and fallback are intentionally out of scope here."""

    def __init__(self, client: LiteLLMClient | None = None) -> None:
        self.client = client or _default_client()

    async def complete(
        self,
        profile: ModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> ModelResponse:
        payload = _request_payload(profile, credential, request)
        try:
            response = await _await_if_needed(self.client.acompletion(**payload))
            return _normalize_response(
                response,
                provider=credential.provider,
                model=profile.model,
            )
        except ModelGatewayError:
            raise
        except BaseException as error:
            raise _normalize_error(error) from error

    async def stream(
        self,
        profile: ModelProfile,
        credential: ProviderCredential,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        payload = _request_payload(profile, credential, request, stream=True)
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: ModelUsage | None = None
        try:
            raw_stream = await _await_if_needed(self.client.acompletion(**payload))
            if hasattr(raw_stream, "__aiter__"):
                async for chunk in raw_stream:
                    for event in _stream_events(
                        chunk, content_parts=content_parts, tool_parts=tool_parts
                    ):
                        yield event
                    usage = _normalize_usage(_value(chunk, "usage")) or usage
                    finish_reason = _stream_finish_reason(chunk) or finish_reason
            else:
                for chunk in raw_stream:
                    for event in _stream_events(
                        chunk, content_parts=content_parts, tool_parts=tool_parts
                    ):
                        yield event
                    usage = _normalize_usage(_value(chunk, "usage")) or usage
                    finish_reason = _stream_finish_reason(chunk) or finish_reason
        except ModelGatewayError:
            raise
        except BaseException as error:
            raise _normalize_error(error) from error
        tool_calls = tuple(_complete_tool_call(parts) for parts in tool_parts.values())
        response = ModelResponse(
            content="".join(content_parts),
            provider=credential.provider,
            model=profile.model,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
        if usage is not None:
            yield ModelStreamEvent(event_type=ModelStreamEventType.USAGE, usage=usage)
        yield ModelStreamEvent(event_type=ModelStreamEventType.COMPLETED, response=response)

    async def health(
        self,
        profile: ModelProfile,
        credential: ProviderCredential,
    ) -> None:
        request = ModelRequest(messages=(ModelMessage(role="user", content="ping"),))
        await self.complete(profile, credential, request)


def _stream_events(
    chunk: Any,
    *,
    content_parts: list[str],
    tool_parts: dict[int, dict[str, Any]],
) -> list[ModelStreamEvent]:
    events: list[ModelStreamEvent] = []
    choices = _value(chunk, "choices", []) or []
    for choice in choices:
        delta = _value(choice, "delta", {})
        content = _value(delta, "content")
        if content:
            content_parts.append(content)
            events.append(
                ModelStreamEvent(
                    event_type=ModelStreamEventType.MESSAGE_DELTA,
                    message_delta=content,
                )
            )
        for tool_call in _value(delta, "tool_calls", []) or []:
            index = int(_value(tool_call, "index", 0))
            function = _value(tool_call, "function", {})
            parts = tool_parts.setdefault(
                index,
                {"name": None, "arguments": [], "provider_tool_call_id": None},
            )
            parts["name"] = _value(function, "name") or parts["name"]
            parts["arguments"].append(_value(function, "arguments", "") or "")
            parts["provider_tool_call_id"] = (
                _value(tool_call, "id") or parts["provider_tool_call_id"]
            )
            events.append(
                ModelStreamEvent(
                    event_type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call_delta=ModelToolCallDelta(
                        index=index,
                        name=parts["name"],
                        arguments_delta=_value(function, "arguments", "") or "",
                        provider_tool_call_id=parts["provider_tool_call_id"],
                    ),
                )
            )
    return events


def _stream_finish_reason(chunk: Any) -> str | None:
    choices = _value(chunk, "choices", []) or []
    return _value(choices[0], "finish_reason") if choices else None


def _complete_tool_call(parts: dict[str, Any]) -> ModelToolCall:
    raw_arguments = "".join(parts["arguments"])
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError as error:
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE) from error
    if not isinstance(arguments, Mapping):
        raise ModelGatewayError(ModelGatewayErrorCode.MODEL_BAD_RESPONSE)
    return ModelToolCall(
        name=parts["name"] or "",
        arguments=dict(arguments),
        provider_tool_call_id=parts["provider_tool_call_id"],
    )


__all__ = ["LiteLLMProviderAdapter", "LiteLLMClient"]
