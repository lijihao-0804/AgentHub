from __future__ import annotations

import json

import httpx
import pytest

from benchmarks.public_rag.scenario_selector import (
    MODEL,
    SCENARIO_CHAIN_PROMPT,
    select_chain_once,
)

pytestmark = pytest.mark.asyncio

CANDIDATES = [
    {"document_id": "doc-a", "title": "Alpha", "text": "A fact"},
    {"document_id": "doc-b", "title": "Beta", "text": "B bridge"},
    {"document_id": "doc-c", "title": "Gamma", "text": "C fact"},
]


def _body(content: str, *, model: str | None = MODEL, usage=None) -> dict:
    body = {"choices": [{"message": {"content": content}}]}
    if model is not None:
        body["model"] = model
    if usage is not None:
        body["usage"] = usage
    return body


async def _run(handler, candidates=CANDIDATES):
    calls = []

    def transport(request):
        calls.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await select_chain_once(
            client, "https://mock.invalid/chat", "secret", "Who made it?", candidates
        )
    return result, calls


async def test_one_request_sends_admitted_raw_sources_and_returns_ordered_documents():
    usage = {"prompt_tokens": 17, "provider_extra": "unknown"}

    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == MODEL
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 256
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert len(payload["messages"]) == 3
        sources = json.loads(payload["messages"][1]["content"])
        assert sources == [
            {"source_id": 0, "title": "Alpha", "text": "A fact"},
            {"source_id": 1, "title": "Beta", "text": "B bridge"},
            {"source_id": 2, "title": "Gamma", "text": "C fact"},
        ]
        assert all(set(source) == {"source_id", "title", "text"} for source in sources)
        assert "doc-" not in payload["messages"][1]["content"]
        return httpx.Response(200, json=_body('{"source_ids":[2,0,1]}', usage=usage))

    result, calls = await _run(handler)

    assert len(calls) == 1
    assert result["status"] == "VALID"
    assert result["selected_document_ids"] == ["doc-c", "doc-a", "doc-b"]
    assert result["usage"] == {"prompt_tokens": 17}
    assert result["response_model_verified"] is True
    assert result["fatal"] is False
    assert result["budget"]["evidence_semantically_preserved"] is True


async def test_legal_empty_selection_and_missing_usage_remain_explicit():
    result, calls = await _run(lambda request: httpx.Response(200, json=_body('{"source_ids":[]}')))

    assert len(calls) == 1
    assert result["status"] == "VALID"
    assert result["selected_document_ids"] == []
    assert result["usage"] is None


@pytest.mark.parametrize(
    "content",
    [
        '{"source_ids":[0,0]}',
        '{"source_ids":[5]}',
        '{"source_ids":[true]}',
        '{"source_ids":[0],"extra":1}',
        '{"source_ids":[0,1,2,3,4,5]}',
        '{"source_ids":[0],"source_ids":[1]}',
    ],
)
async def test_strict_parser_rejects_duplicate_out_of_range_bool_unknown_and_over_limit(
    content,
):
    candidates = CANDIDATES + [
        {"document_id": "doc-d", "title": "", "text": "D"},
        {"document_id": "doc-e", "title": "", "text": "E"},
    ]
    result, calls = await _run(
        lambda request: httpx.Response(200, json=_body(content)), candidates=candidates
    )

    assert len(calls) == 1
    assert result["status"] == "INVALID_OUTPUT"
    assert result["selected_document_ids"] == []
    assert result["fatal"] is False


@pytest.mark.parametrize("model", [None, "different-model"])
async def test_missing_or_mismatched_response_model_is_fatal(model):
    result, calls = await _run(
        lambda request: httpx.Response(200, json=_body('{"source_ids":[0]}', model=model))
    )

    assert len(calls) == 1
    assert result["status"] == "INVALID_OUTPUT"
    assert result["response_model_verified"] is False
    assert result["selected_document_ids"] == []
    assert result["fatal"] is True


@pytest.mark.parametrize(
    ("exception", "error_type"),
    [
        (httpx.ReadTimeout("slow"), "TIMEOUT"),
        (httpx.ConnectError("offline"), "TRANSPORT"),
    ],
)
async def test_timeout_and_transport_are_unknown_without_retry(exception, error_type):
    def handler(request):
        raise exception

    result, calls = await _run(handler)

    assert len(calls) == 1
    assert result["status"] == "UNKNOWN_OUTCOME"
    assert result["error_type"] == error_type
    assert result["selected_document_ids"] == []


async def test_context_budget_change_does_not_call_api():
    huge = [{"document_id": "doc", "title": "", "text": "x" * 40000}]

    def handler(request):
        raise AssertionError("API must not be called")

    result, calls = await _run(handler, candidates=huge)

    assert calls == []
    assert result["status"] == "BUDGET_FALLBACK"
    assert result["selected_document_ids"] == []
    assert result["budget"]["evidence_semantically_preserved"] is False


async def test_http_error_is_fatal_and_prompt_excludes_answer_markers_and_gold():
    result, calls = await _run(lambda request: httpx.Response(429, text="sensitive body"))

    assert len(calls) == 1
    assert result["status"] == "HTTP_ERROR"
    assert result["fatal"] is True
    assert result["selected_document_ids"] == []
    assert "support marker" in SCENARIO_CHAIN_PROMPT
    assert "gold" in SCENARIO_CHAIN_PROMPT
