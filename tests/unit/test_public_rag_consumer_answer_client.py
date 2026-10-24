from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

from benchmarks.public_rag.consumer_answer_client import EXPERIMENT_MODEL, generate_once

pytestmark = pytest.mark.asyncio

BASE_URL = "https://mock.invalid/v1"
MESSAGES = [
    {"role": "system", "content": "Use only supplied evidence."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"fact"}'},
            }
        ],
    },
    {"role": "tool", "content": '{"result":"fact"}', "tool_call_id": "call-1"},
    {"role": "user", "content": "What is the fact?"},
]


def _body(content: str, *, model: object = EXPERIMENT_MODEL, usage: object = None) -> dict:
    body = {"model": model, "choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


async def _run(handler, *, messages: list[dict] | None = None) -> tuple[dict, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await generate_once(
            messages if messages is not None else MESSAGES,
            client=client,
            base_url=BASE_URL,
            api_key="test-secret",
        )
    return result, calls


async def test_request_is_fixed_preserves_tool_history_and_returns_valid_answer() -> None:
    original = deepcopy(MESSAGES)
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
        "prompt_tokens_details": {"cached_tokens": 3, "audio_tokens": False},
        "completion_tokens_details": {"reasoning_tokens": 2, "audio_tokens": 0},
        "cost": "provider-value",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(BASE_URL + "/chat/completions")
        assert request.headers["authorization"] == "Bearer test-secret"
        assert json.loads(request.content) == {
            "model": EXPERIMENT_MODEL,
            "temperature": 0,
            "max_tokens": 256,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": original,
        }
        return httpx.Response(
            200,
            json=_body(
                '{"answer":"The fact.","unanswerable":false,'
                '"cited_chunk_ids":["chunk-a","chunk-b"]}',
                usage=usage,
            ),
        )

    result, calls = await _run(handler)

    assert len(calls) == 1
    assert MESSAGES == original
    assert result["status"] == "VALID"
    assert result["answer"] == "The fact."
    assert result["unanswerable"] is False
    assert result["cited_chunk_ids"] == ["chunk-a", "chunk-b"]
    assert result["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2, "audio_tokens": 0},
    }


async def test_empty_citations_are_legal_and_missing_usage_stays_unknown() -> None:
    result, calls = await _run(
        lambda request: httpx.Response(
            200,
            json=_body(
                '{"answer":"Supported answer.","unanswerable":false,"cited_chunk_ids":[]}'
            ),
        )
    )

    assert len(calls) == 1
    assert result["status"] == "VALID"
    assert result["cited_chunk_ids"] == []
    assert result["usage"] is None


@pytest.mark.parametrize(
    "content",
    [
        "```json\n"
        '{"answer":"A","unanswerable":false,"cited_chunk_ids":[]}'
        "\n```",
        '{"answer":"A","unanswerable":false}',
        '{"answer":"A","unanswerable":false,"cited_chunk_ids":[],"extra":true}',
        '{"answer":1,"unanswerable":false,"cited_chunk_ids":[]}',
        '{"answer":"A","unanswerable":0,"cited_chunk_ids":[]}',
        '{"answer":"A","unanswerable":false,"cited_chunk_ids":"chunk-a"}',
        '{"answer":"A","unanswerable":false,"cited_chunk_ids":[1]}',
        '{"answer":"A","unanswerable":true,"cited_chunk_ids":[]}',
        '{"answer":"","unanswerable":false,"cited_chunk_ids":[]}',
        '{"answer":"   ","unanswerable":false,"cited_chunk_ids":[]}',
        '{"answer":"A","unanswerable":false,"cited_chunk_ids":[""]}',
        '{"answer":"A","answer":"B","unanswerable":false,"cited_chunk_ids":[]}',
        "[]",
    ],
)
async def test_answer_json_is_strict_and_bad_output_retains_reported_usage(content) -> None:
    usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    result, calls = await _run(
        lambda request: httpx.Response(200, json=_body(content, usage=usage))
    )

    assert len(calls) == 1
    assert result["status"] == "INVALID_OUTPUT"
    assert result["usage"] == usage
    assert "answer" not in result


async def test_usage_rejects_boolean_and_string_counts_without_inventing_values() -> None:
    usage = {
        "prompt_tokens": True,
        "completion_tokens": "4",
        "total_tokens": -1,
        "prompt_tokens_details": {"cached_tokens": False},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    result, _ = await _run(
        lambda request: httpx.Response(
            200,
            json=_body(
                '{"answer":"A","unanswerable":false,"cited_chunk_ids":[]}',
                usage=usage,
            ),
        )
    )

    assert result["status"] == "VALID"
    assert result["usage"] == {"completion_tokens_details": {"reasoning_tokens": 3}}


@pytest.mark.parametrize("model", [None, "deepseek-v4.1-flash"])
async def test_missing_or_mismatched_response_model_is_a_model_identity_error(model) -> None:
    usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    result, calls = await _run(
        lambda request: httpx.Response(
            200,
            json=_body(
                '{"answer":"A","unanswerable":false,"cited_chunk_ids":[]}',
                model=model,
                usage=usage,
            ),
        )
    )

    assert len(calls) == 1
    assert result == {
        "status": "MODEL_IDENTITY_ERROR",
        "request_ms": result["request_ms"],
        "usage": usage,
    }
    assert "response_model_verified" not in result


async def test_non_success_response_is_provider_error_without_retry_or_body() -> None:
    result, calls = await _run(
        lambda request: httpx.Response(429, text="sensitive upstream body")
    )

    assert len(calls) == 1
    assert result["status"] == "PROVIDER_ERROR"
    assert result["http_status"] == 429
    assert result["usage"] is None
    assert "sensitive upstream body" not in repr(result)


async def test_timeout_is_unknown_outcome_with_one_attempt_and_no_exception_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret transport detail")

    result, calls = await _run(handler)

    assert len(calls) == 1
    assert result["status"] == "UNKNOWN_OUTCOME"
    assert result["usage"] is None
    assert isinstance(result["request_ms"], float)
    assert result["request_ms"] >= 0
    assert "secret transport detail" not in repr(result)
