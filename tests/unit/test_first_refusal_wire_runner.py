import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from benchmarks.retrieval.experiments.first_refusal_100.attempts import read_attempt, read_http
from benchmarks.retrieval.table_header_cloud_qa import CloudTableMeter
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport


def meter_fixture(monkeypatch, path, *, budget="1", missing=False, choices=True):
    sent = []
    observed = []
    route = SimpleNamespace(model="fixture", base_url="https://provider.invalid/v1")

    async def wire(transport, request):
        sent.append(request.url.path)
        usage = {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
        choice = {
            "message": {
                "role": "assistant",
                "content": "A complete model output.",
                "reasoning_content": "private-fixture-sentinel",
            },
            "finish_reason": "stop",
        }
        return httpx.Response(
            200,
            json={
                "id": str(len(sent)),
                "model": route.model,
                "created": 0,
                "usage": {} if missing else usage,
                "choices": [choice] if choices else [],
            },
        )

    monkeypatch.setattr(GuardedProviderTransport, "handle_async_request", wire)
    meter = CloudTableMeter(
        path, max_calls=8, budget_cny=budget, route=route, attempt_observer=observed.append
    )
    req = httpx.Request(
        "POST",
        route.base_url + "/chat/completions",
        json={
            "model": route.model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Fixture"}],
        },
    )
    return meter, req, sent, observed


@pytest.mark.asyncio
async def test_refuses_the_next_call_before_charging_if_its_reservation_exceeds_cap(
    monkeypatch, tmp_path
):
    ledger = tmp_path / "ledger"
    meter, request, sent, observed = meter_fixture(monkeypatch, ledger, budget="0.018432")
    with meter.measure():
        with meter.request_scope("q/a"):
            await GuardedProviderTransport.handle_async_request(None, request)
        with meter.request_scope("q/b"), pytest.raises(ValueError, match="envelope"):
            await GuardedProviderTransport.handle_async_request(None, request)
    assert len(sent) == len(observed) == 1
    assert meter.confirmed_cny == Decimal("0.000104") and meter.outstanding_cny == 0
    rows = [json.loads(line) for line in ledger.read_bytes().splitlines()]
    assert [(r["state"], r["key"], r["attempt"]) for r in rows] == [
        ("reserved", "q/a", 1),
        ("confirmed", "q/a", 1),
    ]
    assert not any("messages" in r for r in rows)
    assert observed[0]["content"] == "A complete model output." and observed[0]["messages"]
    assert "private-fixture-sentinel" not in json.dumps(observed[0])
    assert set(observed[0]["provider_response"]) == {"id", "model", "created", "usage"}


@pytest.mark.asyncio
async def test_unknown_retains_its_reservation_and_blocks_later_requests(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger"
    meter, request, sent, observed = meter_fixture(monkeypatch, ledger, missing=True)
    with meter.measure():
        with meter.request_scope("q/a"), pytest.raises(ValueError, match="usage"):
            await GuardedProviderTransport.handle_async_request(None, request)
        with pytest.raises(ValueError, match="halted"), meter.request_scope("q/b"):
            pass
    assert len(sent) == 1 and not observed and meter.confirmed_cny == 0
    assert meter.outstanding_cny == meter.reserve
    assert json.loads(ledger.read_bytes().splitlines()[-1]) == {
        "call": 1,
        "key": "q/a",
        "attempt": 1,
        "state": "unconfirmed",
    }
    with pytest.raises(FileExistsError), meter.measure():
        pass


@pytest.mark.asyncio
async def test_allows_one_correction_and_refuses_a_third_attempt(monkeypatch, tmp_path):
    meter, request, sent, observed = meter_fixture(monkeypatch, tmp_path / "ledger")
    with meter.measure(), meter.request_scope("q/a"):
        for _ in range(2):
            await GuardedProviderTransport.handle_async_request(None, request)
        with pytest.raises(ValueError, match="envelope"):
            await GuardedProviderTransport.handle_async_request(None, request)
    assert len(sent) == 2 and [e["attempt"] for e in observed] == [1, 2]
    assert [e["provider_response"]["id"] for e in observed] == ["1", "2"]


@pytest.mark.asyncio
async def test_confirmed_usage_with_no_choice_keeps_known_cost(monkeypatch, tmp_path):
    meter, request, _, observed = meter_fixture(monkeypatch, tmp_path / "ledger", choices=False)
    with meter.measure(), meter.request_scope("q/a"):
        await GuardedProviderTransport.handle_async_request(None, request)
    assert meter.confirmed_cny > 0 and meter.outstanding_cny == 0
    assert observed[0]["content"] == "" and observed[0]["finish_reason"] is None


def test_raw_first_output_truncation_and_refusal_markers_are_not_credited():
    evidence = [{"id": 1, "doc_id": "source", "text": "Oslo"}]

    def project(text, ids, refusal, reason="stop"):
        content = json.dumps(
            {"answer": text, "citation_ids": ids, "insufficient_evidence": refusal}
        )
        return read_attempt({"content": content, "finish_reason": reason}, evidence)

    invalid = project("No location [1].", [1], True)
    assert invalid["state"] == "invalid" and invalid["invalid_reason"] == "citations_on_refusal"
    assert invalid["declared_refusal"] and invalid["declared_ids"] == [1]
    assert project("Oslo [1].", [1], False, "length")["invalid_reason"] == "output_limit"
    assert project("Oslo [1].", [1], False)["citations"] == evidence
    assert project("Not specified.", [], True)["state"] == "refusal"


def test_http_projection_rejects_cross_revision_citations():
    body = {
        "retrieval_trace": {"snapshot_id": "snapshot"},
        "answer": "OK [1].",
        "insufficient_evidence": False,
        "citations": [
            {
                "id": 1,
                "document_id": "doc",
                "document_revision_id": "wrong",
                "chunk_id": "c",
                "locator": {},
                "excerpt": "OK",
            }
        ],
    }
    with pytest.raises(ValueError, match="outside"):
        read_http(
            httpx.Response(200, json=body),
            {"snapshot": "snapshot", "document": "doc", "revision": "correct"},
            "source",
        )
