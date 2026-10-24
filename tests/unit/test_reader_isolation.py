"""Check experimental controls without dispatching a provider request."""

from io import StringIO
from types import SimpleNamespace

import pytest

from benchmarks.public_rag.paragraph_qa_live import RouteClient
from benchmarks.public_rag.reader_isolation import (
    MAX_TOKENS,
    route_for,
    thinking_observed,
    write_new,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking,effort", [("enabled", "high"), ("disabled", "none")])
async def test_thinking_controls_preserve_identity_and_record_usage_without_reasoning(
    monkeypatch, thinking, effort
):
    import litellm

    seen = []

    async def provider(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            model="deepseek-v4.1-flash",
            usage=SimpleNamespace(model_dump=lambda: {"completion_tokens": 23}),
            choices=[
                SimpleNamespace(message=SimpleNamespace(reasoning_content="private reasoning"))
            ],
        )

    monkeypatch.setattr(litellm, "acompletion", provider)
    budget, events = {"dispatched": 0, "limit": 2}, StringIO()
    route = {
        "chain": [
            {
                "base_url": "http://localhost/v1",
                "model": "auto/deepseek-v4.1-flash",
                "name": "primary",
            }
        ]
    }
    client = RouteClient(
        events, {}, budget, route, [], max_tokens=4096, thinking=thinking, reasoning_effort=effort
    )
    kwargs = dict(
        api_base="http://localhost/v1",
        api_key="never-log-this",
        model="openai/auto/deepseek-v4.1-flash",
        max_tokens=4096,
        num_retries=0,
        temperature=0,
    )
    await client.acompletion(**kwargs)
    assert seen[0]["reasoning_effort"] == effort
    assert seen[0]["extra_body"] == {"thinking": {"type": thinking}}
    assert client.raw_calls[0]["provider_usage"] == {"completion_tokens": 23}
    assert client.raw_calls[0]["reasoning_content_chars"] == len("private reasoning")
    assert (
        "private reasoning" not in events.getvalue() and "never-log-this" not in events.getvalue()
    )
    with pytest.raises(ValueError, match="constraints changed"):
        await client.acompletion(**{**kwargs, "max_tokens": 1024})
    assert budget["dispatched"] == 1


def test_registration_never_overwrites_existing_artifact():
    class ExistingPath:
        def open(self, mode, **kwargs):
            assert mode == "x"
            raise FileExistsError("registered")

    with pytest.raises(FileExistsError):
        write_new(ExistingPath(), {"replaced": True})


def test_reader_route_freezes_equal_total_output_ceiling():
    route = route_for()
    assert route.primary.max_tokens == MAX_TOKENS
    assert route.primary.model == "auto/deepseek-v4.1-flash"
    assert all(p.max_tokens == MAX_TOKENS for p in route.fallbacks)


@pytest.mark.asyncio
async def test_uncertain_dispatch_stops_and_cannot_dispatch_fallback(monkeypatch):
    import litellm

    class UncertainError(Exception):
        status_code = 503

    async def provider(**kwargs):
        raise UncertainError("upstream response unavailable")

    monkeypatch.setattr(litellm, "acompletion", provider)
    budget = {"dispatched": 0, "limit": 3}
    route = {
        "chain": [
            {
                "base_url": "http://localhost/v1",
                "model": "auto/deepseek-v4.1-flash",
                "name": "primary",
            }
        ]
    }
    client = RouteClient(StringIO(), {}, budget, route, [], strict_primary=True)
    kwargs = dict(
        api_base="http://localhost/v1",
        model="openai/auto/deepseek-v4.1-flash",
        max_tokens=1024,
        num_retries=0,
        temperature=0,
    )
    with pytest.raises(UncertainError):
        await client.acompletion(**kwargs)
    assert client.raw_calls[0]["status"] == "TRANSPORT_UNCONFIRMED"
    with pytest.raises(ValueError, match="ceiling"):
        await client.acompletion(**kwargs)
    assert budget["dispatched"] == 1
    assert not thinking_observed(client.raw_calls)
    assert thinking_observed(
        [{"provider_usage": {"completion_tokens_details": {"reasoning_tokens": 7}}}]
    )


@pytest.mark.asyncio
async def test_alternate_route_requires_the_exact_registered_raw_model_identity(monkeypatch):
    import litellm

    responses = iter(["glm-5.3", "different-model"])

    async def provider(**kwargs):
        return SimpleNamespace(model=next(responses))

    monkeypatch.setattr(litellm, "acompletion", provider)
    route = {
        "chain": [{"base_url": "http://localhost/v1", "model": "zai/glm-5.3", "name": "primary"}]
    }
    client = RouteClient(
        StringIO(),
        {},
        {"dispatched": 0, "limit": 2},
        route,
        [],
        response_models={"zai/glm-5.3": "glm-5.3"},
    )
    kwargs = dict(
        api_base="http://localhost/v1",
        model="openai/zai/glm-5.3",
        max_tokens=1024,
        num_retries=0,
        temperature=0,
    )
    await client.acompletion(**kwargs)
    assert client.raw_calls[0]["raw_response_model"] == "glm-5.3"
    with pytest.raises(ValueError, match="unexpected raw provider model identity"):
        await client.acompletion(**kwargs)
    assert client.halted and client.raw_calls[-1]["status"] == "MODEL_IDENTITY_ERROR"
