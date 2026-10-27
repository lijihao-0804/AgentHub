"""Route-specific cache support remains distinct from observed usage."""

import pytest

from packages.model_gateway.adapters.litellm.adapter import LiteLLMProviderAdapter
from packages.model_gateway.cache_capability import CacheSupport, prompt_cache_capability
from packages.model_gateway.gateway import ModelGatewayService
from tests.unit.test_litellm_adapter import FakeCompletionClient, profile
from tests.unit.test_model_gateway_service import FakeRepository, setup_chain


def test_direct_documented_route_is_supported_but_not_a_verified_hit():
    model, credential = profile("openai-compatible")
    model.model = "deepseek-v4.1-flash-1017"
    credential.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    result = LiteLLMProviderAdapter(FakeCompletionClient(response={})).prompt_cache_capability(
        model, credential
    )
    assert result.implicit is CacheSupport.SUPPORTED
    assert result.explicit is CacheSupport.UNKNOWN


@pytest.mark.parametrize(
    ("base_url", "model"),
    [
        ("http://127.0.0.1:3425/v1", "auto/deepseek-v4.1-flash"),
        ("http://127.0.0.1:3425/v1", "deepseek-v4.1-flash-1017"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "auto/deepseek-v4.1-flash"),
        (
            "https://dashscope.aliyuncs.com.evil.invalid/compatible-mode/v1",
            "deepseek-v4.1-flash-1017",
        ),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1?proxy=1", "deepseek-v4.1-flash-1017"),
    ],
)
def test_proxy_and_unverified_model_routes_stay_unknown(base_url, model):
    result = prompt_cache_capability(base_url=base_url, model=model)
    assert result.implicit is CacheSupport.UNKNOWN
    assert result.explicit is CacheSupport.UNKNOWN


@pytest.mark.asyncio
async def test_gateway_reports_primary_and_fallback_without_provider_call():
    context, profiles, credential = setup_chain()
    profiles[0].model = "deepseek-v4.1-flash-1017"
    credential.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    client = FakeCompletionClient(response={})
    service = ModelGatewayService(
        FakeRepository(profiles, [credential]), LiteLLMProviderAdapter(client)
    )
    results = await service.prompt_cache_capabilities(context, profiles[0].id)
    assert [item[0] for item in results] == [profiles[0].id, profiles[1].id]
    assert results[0][1].implicit is CacheSupport.SUPPORTED
    assert results[1][1].implicit is CacheSupport.UNKNOWN
    assert client.calls == []
