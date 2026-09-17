from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelStreamEventType,
    ModelToolDefinition,
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
    def __init__(self, response=None, error: BaseException | None = None) -> None:
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
    assert client.calls[0]["tools"][0]["function"]["name"] == "lookup"


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
