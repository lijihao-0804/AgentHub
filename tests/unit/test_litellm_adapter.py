import asyncio
import traceback
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile, ProviderCredential


def profile(provider: str = "deepseek") -> tuple[ModelProfile, ProviderCredential]:
    workspace_id = uuid4()
    credential = ProviderCredential(
        workspace_id=workspace_id,
        provider=provider,
        name="test",
        secret="adapter-secret",
        base_url="https://provider.invalid/v1" if provider == "openai-compatible" else None,
    )
    model_profile = ModelProfile(
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model="deepseek-chat" if provider == "deepseek" else "compatible-model",
        temperature=Decimal("0.2"),
        max_tokens=128,
        timeout_seconds=Decimal("12"),
    )
    return model_profile, credential


class FakeCompletionClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeStreamClient:
    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs) -> AsyncIterator[dict]:
        self.calls.append(kwargs)

        async def chunks() -> AsyncIterator[dict]:
            for chunk in self.chunks:
                yield chunk

        return chunks()


@pytest.mark.asyncio
async def test_closing_adapter_stream_closes_retained_provider_iterator():
    class RetainedStream:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            return {"choices": [{"delta": {"content": "visible"}}]}

        async def aclose(self):
            self.closed = True

    raw = RetainedStream()
    client = FakeCompletionClient(response=raw)
    model_profile, credential = profile()
    stream = LiteLLMProviderAdapter(client).stream(
        model_profile, credential, ModelRequest(messages=(ModelMessage("user", "fixture"),))
    )
    assert (await anext(stream)).message_delta == "visible"
    await stream.aclose()
    assert raw.closed


@pytest.mark.asyncio
async def test_deepseek_request_is_mapped_and_response_is_normalized() -> None:
    client = FakeCompletionClient(
        response={
            "choices": [
                {
                    "message": {"content": "hello", "tool_calls": []},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        }
    )
    model_profile, credential = profile()
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="hi"),),
        tools=(ModelToolDefinition(name="lookup", parameters={"type": "object"}),),
    )

    response = await LiteLLMProviderAdapter(client).complete(model_profile, credential, request)

    assert response.content == "hello"
    assert response.provider == "deepseek"
    assert response.usage is not None and response.usage.total_tokens == 5
    assert client.calls[0]["model"] == "deepseek/deepseek-chat"
    assert client.calls[0]["api_key"] == "adapter-secret"
    assert client.calls[0]["num_retries"] == 0
    assert client.calls[0]["tools"][0]["function"]["name"] == "lookup"


@pytest.mark.asyncio
async def test_litellm_response_objects_preserve_known_usage_and_zero_cache() -> None:
    client = FakeCompletionClient(
        response=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="hello", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=4,
                total_tokens=14,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )
    )
    model_profile, credential = profile("openai-compatible")

    response = await LiteLLMProviderAdapter(client).complete(
        model_profile,
        credential,
        ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
    )

    assert response.usage == ModelUsage(
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
        cached_tokens=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        "malformed",
        {},
        {"prompt_tokens": 2},
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 2, "completion_tokens": False, "total_tokens": 2},
        {"prompt_tokens": "2", "completion_tokens": 2, "total_tokens": 4},
        {"prompt_tokens": 2.0, "completion_tokens": 2, "total_tokens": 4},
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
    ],
)
async def test_untrusted_core_usage_fails_closed(usage) -> None:
    client = FakeCompletionClient(
        response={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": usage,
            "response_cost": "0.0001",
        }
    )
    model_profile, credential = profile()

    response = await LiteLLMProviderAdapter(client).complete(
        model_profile,
        credential,
        ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
    )

    assert response.usage is None
    if usage is not None:
        assert response.cost_estimate is None


@pytest.mark.asyncio
async def test_missing_total_can_be_calculated_from_input_and_output() -> None:
    client = FakeCompletionClient(
        response={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": None,
            },
        }
    )
    model_profile, credential = profile()

    response = await LiteLLMProviderAdapter(client).complete(
        model_profile,
        credential,
        ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
    )

    assert response.usage == ModelUsage(
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("total_tokens", [True, "14", -1, 99])
async def test_contradictory_total_cannot_be_used_for_accounting(total_tokens) -> None:
    client = FakeCompletionClient(
        response={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": total_tokens},
        }
    )
    model_profile, credential = profile()
    response = await LiteLLMProviderAdapter(client).complete(
        model_profile,
        credential,
        ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
    )
    assert response.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_tokens", [False, "4", -1, 11])
async def test_untrusted_cache_usage_is_unknown_without_dropping_core_usage(
    cached_tokens,
) -> None:
    client = FakeCompletionClient(
        response={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": cached_tokens},
            },
        }
    )
    model_profile, credential = profile()

    response = await LiteLLMProviderAdapter(client).complete(
        model_profile,
        credential,
        ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
    )

    assert response.usage == ModelUsage(
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
    )


@pytest.mark.asyncio
async def test_tool_result_message_preserves_provider_tool_call_id() -> None:
    client = FakeCompletionClient(
        response={
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        }
    )
    model_profile, credential = profile()
    request = ModelRequest(
        messages=(
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ModelToolCall(
                        name="lookup",
                        arguments={"q": "x"},
                        provider_tool_call_id="call-1",
                    ),
                ),
            ),
            ModelMessage(role="tool", content='{"result": "ok"}', tool_call_id="call-1"),
        )
    )

    await LiteLLMProviderAdapter(client).complete(model_profile, credential, request)

    messages = client.calls[0]["messages"]
    assert messages[0]["tool_calls"][0]["id"] == "call-1"
    assert messages[1] == {
        "role": "tool",
        "content": '{"result": "ok"}',
        "tool_call_id": "call-1",
    }


@pytest.mark.asyncio
async def test_replayed_tool_arguments_are_stable_without_reordering_arrays() -> None:
    client = FakeCompletionClient(
        response={"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}
    )
    model_profile, credential = profile()
    arguments = (
        {"z": [2, 1], "a": {"y": 2, "x": 1}},
        {"a": {"x": 1, "y": 2}, "z": [2, 1]},
    )
    for value in arguments:
        await LiteLLMProviderAdapter(client).complete(
            model_profile,
            credential,
            ModelRequest(
                messages=(
                    ModelMessage(
                        role="assistant",
                        tool_calls=(ModelToolCall("lookup", value, "call-1"),),
                    ),
                )
            ),
        )
    first, second = (call["messages"][0]["tool_calls"][0] for call in client.calls)
    assert first == second
    assert first["function"]["arguments"] == '{"a":{"x":1,"y":2},"z":[2,1]}'


@pytest.mark.asyncio
async def test_openai_compatible_stream_is_typed_and_single_attempt() -> None:
    client = FakeStreamClient(
        [
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                },
            },
        ]
    )
    model_profile, credential = profile("openai-compatible")
    events = [
        event
        async for event in LiteLLMProviderAdapter(client).stream(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )
    ]

    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.USAGE,
        ModelStreamEventType.COMPLETED,
    ]
    assert events[-1].response is not None
    assert events[-1].response.content == "hello"
    assert client.calls[0]["model"] == "openai/compatible-model"
    assert client.calls[0]["api_base"] == "https://provider.invalid/v1"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_litellm_stream_objects_preserve_reported_usage() -> None:
    client = FakeStreamClient(
        [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="hel"))],
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="lo"))],
            ),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=4,
                    total_tokens=14,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                ),
            ),
        ]
    )
    model_profile, credential = profile("openai-compatible")

    events = [
        event
        async for event in LiteLLMProviderAdapter(client).stream(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )
    ]

    assert events[-1].response is not None
    assert events[-1].response.usage == ModelUsage(
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
        cached_tokens=0,
    )


@pytest.mark.asyncio
async def test_missing_stream_usage_keeps_last_reported_usage() -> None:
    client = FakeStreamClient(
        [
            {"choices": [{"delta": {"content": "done"}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
            {"choices": []},
        ]
    )
    model_profile, credential = profile()

    events = [
        event
        async for event in LiteLLMProviderAdapter(client).stream(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )
    ]

    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.USAGE,
        ModelStreamEventType.COMPLETED,
    ]
    assert events[-1].response is not None
    assert events[-1].response.usage == ModelUsage(
        input_tokens=10,
        output_tokens=4,
        total_tokens=14,
    )


@pytest.mark.asyncio
async def test_final_malformed_stream_usage_clears_stale_usage() -> None:
    client = FakeStreamClient(
        [
            {"choices": [{"delta": {"content": "done"}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
                "response_cost": "0.0042",
            },
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": True,
                    "completion_tokens": 4,
                    "total_tokens": 5,
                },
                "response_cost": "0.0001",
            },
            {"choices": [], "response_cost": "0.0001"},
        ]
    )
    model_profile, credential = profile()

    events = [
        event
        async for event in LiteLLMProviderAdapter(client).stream(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )
    ]

    assert [event.event_type for event in events] == [
        ModelStreamEventType.MESSAGE_DELTA,
        ModelStreamEventType.COMPLETED,
    ]
    assert events[-1].response is not None
    assert events[-1].response.usage is None
    assert events[-1].response.cost_estimate is None
    assert events[-1].response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_cost_is_attached_to_completed_response() -> None:
    client = FakeStreamClient(
        [
            {"choices": [{"delta": {"content": "done"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "response_cost": "0.0042",
            },
        ]
    )
    model_profile, credential = profile()

    events = [
        event
        async for event in LiteLLMProviderAdapter(client).stream(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )
    ]

    assert events[-1].response is not None
    assert events[-1].response.cost_estimate is not None
    assert events[-1].response.cost_estimate.amount == Decimal("0.0042")


@pytest.mark.asyncio
async def test_provider_exception_traceback_does_not_expose_raw_secret() -> None:
    client = FakeCompletionClient(error=RuntimeError("provider body super-secret-key"))
    model_profile, credential = profile()

    with pytest.raises(ModelGatewayError) as raised:
        await LiteLLMProviderAdapter(client).complete(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )

    assert "super-secret-key" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.asyncio
async def test_provider_cancellation_is_not_normalized() -> None:
    client = FakeCompletionClient(error=asyncio.CancelledError("super-secret-key"))
    model_profile, credential = profile()

    with pytest.raises(asyncio.CancelledError):
        await LiteLLMProviderAdapter(client).complete(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )


@pytest.mark.asyncio
async def test_provider_error_is_normalized_without_raw_secret() -> None:
    error = RuntimeError("provider body contains adapter-secret")
    error.status_code = 429
    client = FakeCompletionClient(error=error)
    model_profile, credential = profile()

    with pytest.raises(ModelGatewayError) as raised:
        await LiteLLMProviderAdapter(client).complete(
            model_profile,
            credential,
            ModelRequest(messages=(ModelMessage(role="user", content="hi"),)),
        )

    assert raised.value.code == ModelGatewayErrorCode.MODEL_RATE_LIMITED
    assert raised.value.retryable is True
    assert "adapter-secret" not in str(raised.value)
