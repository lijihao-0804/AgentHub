"""Exercise the meter around HTTPX and the real guard, stubbing only the httpcore pool."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpcore
import httpx

from packages.knowledge import citation_qa
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport
from packages.model_gateway.egress import parse_provider_endpoint

from .wire import MODEL, Wire, digest, receipt_cost, reservation

FAILURES = ("status", "timeout", "missing_usage", "inconsistent_usage", "duplicate_id")


class MemoryResponseStream:
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content

    async def aclose(self):
        pass


async def scenario(failure, directory):
    route = SimpleNamespace(base_url="https://fixture.invalid/v1")
    url = route.base_url + "/chat/completions"
    transport = GuardedProviderTransport(parse_provider_endpoint(url), policy=None)
    messages = [
        {"role": "system", "content": citation_qa.citation_system_prompt("PROMPT_JSON")},
        {"role": "user", "content": "X" * 8500},
    ]
    body = {"model": MODEL, "temperature": 0, "max_tokens": 256, "messages": messages}
    first = httpx.Request("POST", url, json=body)
    bound = len(first.content) + 128
    plan = {
        "case/candidate": dict(
            message_sha=digest(messages), first_bound=bound, correction_bound=bound + 4096
        )
    }
    receipt = {
        "id": "fixture",
        "created": 0,
        "model": MODEL,
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 3,
            "total_tokens": 1003,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
        "choices": [{"finish_reason": "stop", "message": {"content": "fixture"}}],
    }
    expected_error = ValueError
    expected_stage = "usage"
    expected_status = 200
    if failure == "status":
        result = httpcore.Response(503, content=MemoryResponseStream(b"private provider error"))
        expected_stage, expected_status = "http_status", 503
    elif failure == "timeout":
        result = httpcore.ReadTimeout("simulated")
        expected_error = httpcore.ReadTimeout
        expected_stage, expected_status = "transport", None
    else:
        second = dict(receipt)
        if failure == "missing_usage":
            second.pop("usage")
            second["id"] = "second"
        elif failure == "inconsistent_usage":
            second["usage"] = dict(receipt["usage"], total_tokens=999)
            second["id"] = "second"
        elif failure != "duplicate_id":
            raise ValueError("Unknown measurement fixture")
        result = httpcore.Response(200, content=MemoryResponseStream(json.dumps(second).encode()))
    pool = AsyncMock(
        side_effect=[
            httpcore.Response(200, content=MemoryResponseStream(json.dumps(receipt).encode())),
            result,
        ]
    )
    path = directory / f"composite-wire-check-{uuid4()}.jsonl"
    try:
        with (
            path.open("x", encoding="utf8") as ledger,
            patch.object(transport._pool, "handle_async_request", pool),
        ):
            meter = Wire(route, True, ledger, plan)
            meter.key = "case/candidate"
            meter.visible_choices = {messages[1]["content"]: []}
            async with httpx.AsyncClient(transport=transport) as client:
                with meter.intercept():
                    response = await client.send(first)
                    assert response.request is first and response.status_code == 200
                    corrected = {
                        **body,
                        "messages": messages
                        + [
                            {"role": "assistant", "content": "fixture"},
                            {"role": "user", "content": citation_qa._PROMPT_CITATION_CORRECTION},
                        ],
                    }
                    try:
                        await client.post(url, json=corrected)
                    except expected_error:
                        pass
                    else:
                        raise AssertionError("Unconfirmed exchange was accepted")
                    try:
                        await client.post(url, json=corrected)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("Unconfirmed exchange was retried")
        events = [json.loads(line) for line in path.read_bytes().splitlines()]
        assert pool.await_count == 2 and meter.halted
        assert [e["state"] for e in events] == ["reserved", "confirmed", "reserved", "unknown"]
        assert events[0]["cny"] == str(reservation(bound)) and bound > 8192
        unknown = events[-1]
        assert unknown["failure_stage"] == expected_stage
        assert unknown["http_status"] == expected_status
        assert unknown["failure_type"] == expected_error.__name__
        assert "private provider error" not in path.read_text()
    finally:
        path.unlink(missing_ok=True)


async def check(directory):
    # 1,000 input including 100 cached, 3 output. Epoch UTC is 08:00 Beijing.
    state = dict(
        id="price-fixture",
        model=MODEL,
        created=0,
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 3,
            "total_tokens": 1003,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
    )
    assert receipt_cost(state, MODEL, max_input=1000, max_output=256)[3] == Decimal("0.001828")
    state["created"] = 14 * 3600  # 22:00 Beijing.
    assert receipt_cost(state, MODEL, max_input=1000, max_output=256)[3] == Decimal("0.000914")
    for mode in FAILURES:
        await scenario(mode, directory)
    print("Five raw transport fail-stop scenarios passed, with zero network dispatch.")


if __name__ == "__main__":
    from .inputs import STUDY

    run_directory = STUDY / "zero-network-meter"
    run_directory.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(run_directory))
