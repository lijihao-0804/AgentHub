"""Empty completions are transport failures; refusal remains authoritative."""

import pytest

from packages.model_gateway.contracts import ModelResponse, ModelStreamEvent, ModelStreamEventType
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from tests.unit.test_model_gateway_service import (
    FakeAdapter,
    FakeRepository,
    request,
    response,
    setup_chain,
)


@pytest.mark.asyncio
async def test_empty_primary_completion_uses_fallback():
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.complete_outcomes = {
        "primary": [ModelResponse(content="  ", provider="deepseek", model="primary")],
        "fallback": [response("fallback")],
    }
    result = await ModelGatewayService(FakeRepository(profiles, [credential]), adapter).generate(
        context, profiles[0].id, request()
    )
    assert result.content == "fallback"
    assert adapter.complete_calls == ["primary", "fallback"]


@pytest.mark.asyncio
async def test_refusal_is_not_routed_to_another_model():
    context, profiles, credential = setup_chain()
    adapter = FakeAdapter()
    adapter.complete_outcomes = {
        "primary": [
            ModelResponse(
                content="", provider="deepseek", model="primary", finish_reason="content_filter"
            )
        ],
        "fallback": [response("fallback")],
    }
    result = await ModelGatewayService(FakeRepository(profiles, [credential]), adapter).generate(
        context, profiles[0].id, request()
    )
    assert result.finish_reason == "content_filter"
    assert adapter.complete_calls == ["primary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [False, True])
async def test_empty_stream_falls_back_only_before_visibility(visible):
    context, profiles, credential = setup_chain()

    class Adapter(FakeAdapter):
        async def stream(self, profile, credential, request):
            self.stream_calls.append(profile.model)
            if profile.model == "primary":
                if visible:
                    yield ModelStreamEvent(
                        event_type=ModelStreamEventType.MESSAGE_DELTA, message_delta="partial"
                    )
                yield ModelStreamEvent(
                    event_type=ModelStreamEventType.COMPLETED,
                    response=ModelResponse(content="", provider="deepseek", model="primary"),
                )
            else:
                yield ModelStreamEvent(
                    event_type=ModelStreamEventType.COMPLETED, response=response("fallback")
                )

    adapter = Adapter()
    gateway = ModelGatewayService(FakeRepository(profiles, [credential]), adapter)
    if visible:
        with pytest.raises(ModelGatewayError) as error:
            async for _ in gateway.stream(context, profiles[0].id, request()):
                pass
        assert error.value.code is ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED
        assert adapter.stream_calls == ["primary"]
    else:
        events = [event async for event in gateway.stream(context, profiles[0].id, request())]
        assert events[-1].response.content == "fallback"
        assert adapter.stream_calls == ["primary", "fallback"]
