from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.model_gateway.adapters.litellm import (
    AliyunDeepSeekRequestMode,
    LiteLLMProviderAdapter,
)
from packages.model_gateway.contracts import ModelMessage, ModelRequest
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile, ProviderCredential

ALIYUN_BASE_URL = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
NON_THINKING_MODEL = "deepseek-v4.1-flash-1017"


class RecordingCompletionClient:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        }
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class RecordingStreamClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def acompletion(self, **kwargs) -> AsyncIterator[dict]:
        self.calls.append(kwargs)

        async def chunks() -> AsyncIterator[dict]:
            yield {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}
            yield {
                "choices": [],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            }

        return chunks()


def route(
    *,
    provider: str = "openai-compatible",
    model: str = NON_THINKING_MODEL,
    base_url: str = ALIYUN_BASE_URL,
) -> tuple[ModelProfile, ProviderCredential]:
    workspace_id = uuid4()
    credential = ProviderCredential(
        workspace_id=workspace_id,
        provider=provider,
        name="aliyun-beijing",
        secret="not-for-output",
        base_url=base_url,
    )
    profile = ModelProfile(
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        model=model,
        temperature=Decimal("0"),
        max_tokens=64,
        timeout_seconds=Decimal("12"),
    )
    return profile, credential


def request(
    mode: AliyunDeepSeekRequestMode = AliyunDeepSeekRequestMode.PROVIDER_DEFAULT,
) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="Reply with OK."),),
        request_mode=mode,
    )


def test_request_mode_is_an_exact_provider_default_enum_by_default() -> None:
    built = request()

    assert built.request_mode is AliyunDeepSeekRequestMode.PROVIDER_DEFAULT
    assert built.request_mode.value == "provider_default"


def test_request_mode_rejects_raw_or_unrelated_values() -> None:
    for value in ("provider_default", "non_thinking", "NON_THINKING"):
        with pytest.raises(TypeError):
            ModelRequest(
                messages=(ModelMessage(role="user", content="Reply with OK."),),
                request_mode=value,  # type: ignore[arg-type]
            )


def test_adapters_have_no_constructor_level_request_mode() -> None:
    with pytest.raises(TypeError):
        LiteLLMProviderAdapter(  # type: ignore[call-arg]
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING
        )


@pytest.mark.asyncio
async def test_provider_default_does_not_send_thinking_override() -> None:
    client = RecordingCompletionClient()
    profile, credential = route()

    await LiteLLMProviderAdapter(client).complete(profile, credential, request())

    assert "extra_body" not in client.calls[0]
    assert "enable_thinking" not in client.calls[0]


@pytest.mark.asyncio
async def test_verified_route_explicitly_disables_thinking_without_retry() -> None:
    client = RecordingCompletionClient()
    profile, credential = route()
    adapter = LiteLLMProviderAdapter(client)

    response = await adapter.complete(
        profile,
        credential,
        request(AliyunDeepSeekRequestMode.NON_THINKING),
    )

    assert response.content == "OK"
    assert len(client.calls) == 1
    assert client.calls[0]["extra_body"] == {"enable_thinking": False}
    assert client.calls[0]["num_retries"] == 0
    assert "enable_thinking" not in client.calls[0]


@pytest.mark.asyncio
async def test_request_mode_is_applied_independently_per_call() -> None:
    client = RecordingCompletionClient()
    profile, credential = route()
    adapter = LiteLLMProviderAdapter(client)

    await adapter.complete(profile, credential, request())
    await adapter.complete(
        profile,
        credential,
        request(AliyunDeepSeekRequestMode.NON_THINKING),
    )
    await adapter.complete(profile, credential, request())

    assert "extra_body" not in client.calls[0]
    assert client.calls[1]["extra_body"] == {"enable_thinking": False}
    assert "extra_body" not in client.calls[2]


@pytest.mark.asyncio
async def test_stream_uses_the_same_per_request_non_thinking_payload() -> None:
    client = RecordingStreamClient()
    profile, credential = route()

    events = [
        event
        async for event in LiteLLMProviderAdapter(client).stream(
            profile,
            credential,
            request(AliyunDeepSeekRequestMode.NON_THINKING),
        )
    ]

    assert events[-1].response is not None
    assert events[-1].response.content == "OK"
    assert len(client.calls) == 1
    assert client.calls[0]["extra_body"] == {"enable_thinking": False}
    assert client.calls[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("deepseek", NON_THINKING_MODEL, ALIYUN_BASE_URL),
        ("openai", NON_THINKING_MODEL, ALIYUN_BASE_URL),
        ("openai-compatible", "deepseek-v4.1-flash", ALIYUN_BASE_URL),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://workspace.cn-beijing.maas.aliyuncs.com.evil.example/compatible-mode/v1",
        ),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://user:secret@workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://workspace.cn-beijing.maas.aliyuncs.com:443/compatible-mode/v1",
        ),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1#fragment",
        ),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://workspace.cn-shanghai.maas.aliyuncs.com/compatible-mode/v1",
        ),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v2",
        ),
        (
            "openai-compatible",
            NON_THINKING_MODEL,
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1?target=other",
        ),
    ],
)
async def test_non_thinking_rejects_unverified_route_before_provider_call(
    provider: str,
    model: str,
    base_url: str,
) -> None:
    client = RecordingCompletionClient()
    profile, credential = route(provider=provider, model=model, base_url=base_url)

    with pytest.raises(ModelGatewayError) as raised:
        await LiteLLMProviderAdapter(client).complete(
            profile,
            credential,
            request(AliyunDeepSeekRequestMode.NON_THINKING),
        )

    assert raised.value.code is ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH
    assert client.calls == []
