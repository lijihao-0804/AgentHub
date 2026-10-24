import asyncio
import json

import httpx
import pytest

from benchmarks.public_rag.onyx_selection_runner import (
    MODEL,
    admitted_request,
    observed_usage,
    select_once,
)


def inputs():
    baseline = [
        {"chunk_id": "a", "document_id": "A", "score": -0.5},
        {"chunk_id": "b", "document_id": "B", "score": -1.0},
    ]
    sections = [
        {
            "section_id": i,
            "source_id": hit["document_id"],
            "title": hit["document_id"],
            "content": "Matching scientific evidence.",
            "chunk_ids": [hit["chunk_id"]],
        }
        for i, hit in enumerate(baseline)
    ]
    return baseline, sections


@pytest.mark.parametrize(
    "case", ["valid", "blank_title", "invalid", "timeout", "http", "model_mismatch"]
)
def test_one_attempt_status_and_exact_fallback(case):
    baseline, sections = inputs()
    if case == "blank_title":
        for section in sections:
            section["title"] = ""
    calls, starts = [], []

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == MODEL
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        calls.append(body)
        if case == "timeout":
            raise httpx.ReadTimeout("response not confirmed", request=request)
        if case == "http":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "model": "different-model" if case == "model_mismatch" else MODEL,
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"section_ids": [99] if case == "invalid" else [1]}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await select_once(
                client,
                "https://benchmark.invalid/chat/completions",
                "test-key",
                "query",
                sections,
                baseline,
                on_attempt=lambda: starts.append(True),
            )

    result, fatal = asyncio.run(run())
    assert len(calls) == len(starts) == 1
    expected = {
        "valid": "VALID",
        "blank_title": "VALID",
        "invalid": "INVALID_OUTPUT",
        "timeout": "UNKNOWN_OUTCOME",
        "http": "HTTP_ERROR",
        "model_mismatch": "HTTP_ERROR",
    }[case]
    assert result["status"] == expected
    assert fatal == (case in {"http", "model_mismatch"})
    assert result["attempted"]
    assert result["final_ranking"] == (
        [baseline[1], baseline[0]] if case in {"valid", "blank_title"} else baseline
    )
    assert result["request_ms"] >= 0
    if case in {"valid", "blank_title", "invalid", "model_mismatch"}:
        assert result["usage"]["total_tokens"] == 14
    else:
        assert result["usage"] is None


def test_budget_changed_evidence_causes_no_request():
    baseline, sections = inputs()
    sections[0]["content"] = "x" * 70000
    request, budget = admitted_request("query", sections)
    assert request is None
    assert budget["estimated_input_after"] <= budget["available_input"]

    def handler(request):
        raise AssertionError("budget fallback must not call API")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await select_once(
                client,
                "https://benchmark.invalid",
                "test-key",
                "query",
                sections,
                baseline,
                on_attempt=lambda: pytest.fail("no attempt"),
            )

    result, fatal = asyncio.run(run())
    assert result["status"] == "BUDGET_FALLBACK"
    assert not result["attempted"] and not fatal
    assert result["final_ranking"] == baseline


def test_usage_does_not_invent_missing_or_malformed_token_counts():
    assert observed_usage(None) is None
    assert observed_usage({"prompt_tokens": True, "completion_tokens": "42"}) is None
    assert observed_usage(
        {
            "prompt_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 3, "unknown": "not a count"},
        }
    ) == {"prompt_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 3}}
