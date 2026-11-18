from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from benchmarks.retrieval.table_header_answers import receipt_cost
from benchmarks.retrieval.table_header_cloud_qa import CloudTableMeter
from benchmarks.retrieval.table_header_local_qa import check_grammar_log, score
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport


def test_configured_provider_receipt_prices_cache_and_requires_complete_usage():
    body = {
        "id": "receipt",
        "model": "test",
        "created": 0,  # 08:00 Beijing
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 10,
            "total_tokens": 210,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
    }
    assert receipt_cost(body, "test") == (200, 10, 100, Decimal("0.000300"))
    body["created"] += 14 * 3600
    assert receipt_cost(body, "test")[-1] == Decimal("0.000150")
    del body["usage"]["prompt_tokens_details"]
    with pytest.raises(ValueError, match="Incomplete"):
        receipt_cost(body, "test")


@pytest.mark.asyncio
async def test_missing_wire_receipt_blocks_all_later_dispatches(monkeypatch, tmp_path):
    route = SimpleNamespace(model="test", base_url="https://example.test/v1")
    monkeypatch.setattr("benchmarks.retrieval.table_header_cloud_qa.aliyun_route", lambda: route)
    sent = []

    async def missing_usage(transport, request):
        sent.append(request.url)
        return httpx.Response(200, json={"model": "test"})

    monkeypatch.setattr(GuardedProviderTransport, "handle_async_request", missing_usage)
    request = httpx.Request(
        "POST", route.base_url + "/chat/completions", json={"model": route.model, "max_tokens": 256}
    )
    ledger = tmp_path / "ledger"
    meter = CloudTableMeter(ledger)
    with meter.measure():
        for _ in range(2):
            with pytest.raises(ValueError):
                await GuardedProviderTransport.handle_async_request(None, request)
    assert len(sent) == 1 and meter.halted
    assert '"state": "unconfirmed"' in ledger.read_text()
    with pytest.raises(FileExistsError), meter.measure():
        pass


def test_generation_cannot_pass_with_competing_numbers_or_wrong_cell() -> None:
    citation = {
        "locator": {"page": 1, "cells": [{"row": 3, "column": 2, "value_start": 3, "char_end": 9}]},
        "excerpt": "A: 50,000",
    }

    def answer(text, cited):
        return httpx.Response(
            200, json={"answer": text, "insufficient_evidence": False, "citations": cited}
        )

    assert score(answer("50000 [1].", [citation]), "50,000", [0, 3, 2])["joint"]
    assert not score(answer("50,000 or 30,000 [1].", [citation]), "50,000", [0, 3, 2])["joint"]
    assert not score(answer("50000 [1].", [citation]), "50,000", [0, 3, 4])["joint"]


def test_error_or_empty_answer_is_not_a_correct_refusal() -> None:
    error = httpx.Response(
        502,
        json={
            "error": {"code": "CITATION_QA_INVALID_MODEL_OUTPUT", "message": "Invalid citation."}
        },
    )
    assert not score(error, None, None)["joint"]
    response = httpx.Response(
        200, json={"answer": "", "insufficient_evidence": False, "citations": []}
    )
    assert not score(response, None, None)["joint"]


def test_schema_test_fails_on_provider_grammar_fallback(tmp_path):
    path = tmp_path / "llama.stderr"
    path.write_bytes(b"failed to parse grammar\n")
    offset = path.stat().st_size
    with path.open("ab") as file:
        file.write(b"request: POST /v1/chat/completions 200\n")
    check_grammar_log(path, offset)
    with path.open("ab") as file:
        file.write(b"parse: error parsing grammar: repetition exceeds bound\n")
    with pytest.raises(RuntimeError, match="discarded the decoder grammar"):
        check_grammar_log(path, offset)
