import asyncio
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from benchmarks.evaluation.experiments.customer_quality_20261105.budget import (
    DISPATCHED,
    SETTLED,
    UNKNOWN_OUTCOME,
    BudgetHalted,
    CallStateConflict,
    ResearchBudgetLedger,
)
from benchmarks.evaluation.experiments.customer_quality_20261105.provider import (
    MODEL,
    BudgetedAliyunClient,
    BudgetedProviderError,
)
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import ModelMessage, ModelRequest, ModelToolDefinition

BASE_URL = "https://synthetic-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
API_KEY = "synthetic-test-key-must-not-be-recorded"
CASE_ID = "52f0cfa1-7794-4af4-88f1-ccad9518f0ae"


@pytest.mark.asyncio
async def test_real_transport_boundary_never_retries_or_follows_redirects(monkeypatch):
    import httpx

    from benchmarks.evaluation.experiments.customer_quality_20261105.provider import (
        _single_http_completion,
    )

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://unapproved.example/model"})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: transport)
    with pytest.raises(httpx.HTTPStatusError):
        await _single_http_completion(
            model="openai/" + MODEL,
            api_base=BASE_URL,
            api_key=API_KEY,
            timeout=1,
            messages=[],
            max_tokens=2048,
            stream=False,
        )
    assert len(requests) == 1
    assert requests[0].url.host.endswith(".cn-beijing.maas.aliyuncs.com")
    assert json.loads(requests[0].content)["model"] == MODEL


class RecordingClient:
    def __init__(self, ledger: ResearchBudgetLedger) -> None:
        self.ledger = ledger
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        with sqlite3.connect(self.ledger.db_path) as connection:
            status = connection.execute(
                "SELECT status FROM research_budget_calls ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert status == DISPATCHED
        return _provider_response()


class FailingClient:
    def __init__(self, error_name: str) -> None:
        self.error_name = error_name
        self.calls = 0

    async def __call__(self, **_kwargs):
        self.calls += 1
        if self.error_name == "cancelled":
            raise asyncio.CancelledError
        raise RuntimeError(f"provider exception {API_KEY}")


def _provider_response():
    return SimpleNamespace(
        model=MODEL,
        usage=SimpleNamespace(
            prompt_tokens=123,
            completion_tokens=45,
            total_tokens=168,
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="Synthetic reply",
                    tool_calls=[
                        SimpleNamespace(
                            id="provider-call-id-not-a-logical-key",
                            type="function",
                            function=SimpleNamespace(
                                name="lookup_policy",
                                arguments='{"topic":"refund"}',
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
    )


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(
            ModelMessage(
                role="system",
                content="Synthetic policy.",
            ),
            ModelMessage(role="user", content="Synthetic customer request."),
        ),
        tools=(
            ModelToolDefinition(
                name="lookup_policy",
                description="Synthetic lookup tool.",
                parameters={
                    "type": "object",
                    "properties": {"topic": {"type": "string"}},
                },
            ),
        ),
    )


def _payload() -> dict[str, object]:
    return {
        "model": f"openai/{MODEL}",
        "api_base": BASE_URL,
        "api_key": API_KEY,
        "num_retries": 0,
        "stream": False,
        "max_tokens": 2_048,
        "temperature": 0.0,
        "timeout": 30.0,
        "messages": [{"role": "user", "content": "Synthetic customer request."}],
        "tools": [],
    }


def _profile():
    return SimpleNamespace(
        model=MODEL,
        temperature=0,
        max_tokens=2_048,
        timeout_seconds=30,
    )


def _credential():
    return SimpleNamespace(
        provider="openai-compatible",
        base_url=BASE_URL,
        secret=API_KEY,
    )


def _all_mapping_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_mapping_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_mapping_keys(item)


def _metadata(phase):
    return {
        "phase": phase,
        "case_id": CASE_ID,
        "variant": "candidate",
        "model": MODEL,
        "provider": "aliyun",
    }


@pytest.mark.asyncio
async def test_success_dispatches_only_after_durable_mark_and_settles_usage(tmp_path):
    ledger = ResearchBudgetLedger(tmp_path / "budget.sqlite3")
    fake = RecordingClient(ledger)
    meter = BudgetedAliyunClient(
        ledger=ledger,
        base_url=BASE_URL,
        private_dir=tmp_path / "private",
        client=fake,
    )
    adapter = LiteLLMProviderAdapter(client=meter)
    runtime_key = f"case/{CASE_ID}/runtime"
    judge_key = f"case/{CASE_ID}/judge"

    with meter.scope(runtime_key, _metadata("runtime")):
        runtime_response = await adapter.complete(_profile(), _credential(), _request())
    with meter.scope(judge_key, _metadata("judge")):
        judge_response = await adapter.complete(_profile(), _credential(), _request())

    assert runtime_response.content == judge_response.content == "Synthetic reply"
    assert runtime_response.usage is not None
    assert runtime_response.usage.total_tokens == 168
    assert len(fake.calls) == 2
    assert fake.calls[0]["api_base"] == BASE_URL
    assert fake.calls[0]["model"] == f"openai/{MODEL}"
    assert fake.calls[0]["num_retries"] == 0
    assert fake.calls[0]["stream"] is False
    assert fake.calls[0]["max_tokens"] == 2_048
    assert fake.calls[0]["temperature"] == 0
    assert "extra_body" not in fake.calls[0]

    summary = ledger.summary()
    assert summary["status_counts"][SETTLED] == 2
    assert summary["dispatch_marks"] == 2
    assert summary["actual_settled_cny"] == Decimal("0.001212")
    assert summary["held_reserved_cny"] == 0
    assert summary["halted"] is False
    assert len(meter.records) == 2
    assert [record["metadata"]["phase"] for record in meter.records] == ["runtime", "judge"]
    assert {record["logical_call_key"] for record in meter.records} == {
        f"{runtime_key}/1",
        f"{judge_key}/1",
    }
    attempt = meter.records[1]
    assert attempt["metadata"]["case_id"] == CASE_ID
    assert attempt["usage"] == {
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "total_tokens": 168,
    }
    assert attempt["actual_cny"] == Decimal("0.000606")
    assert attempt["estimated_cny"] == Decimal("5.242880")
    assert attempt["status"] == SETTLED
    assert attempt["latency_ms"] >= 0
    assert "content" not in attempt
    assert "messages" not in attempt
    assert API_KEY not in repr(attempt)
    assert BASE_URL not in repr(attempt)
    with sqlite3.connect(ledger.db_path) as connection:
        logical_keys = {
            row[0]
            for row in connection.execute("SELECT logical_call_key FROM research_budget_calls")
        }
    assert logical_keys == {f"{runtime_key}/1", f"{judge_key}/1"}

    evidence_files = list((tmp_path / "private").glob("*.json"))
    assert len(evidence_files) == 2
    serialized_records = [
        evidence_file.read_text(encoding="utf-8") for evidence_file in evidence_files
    ]
    evidence = [json.loads(serialized) for serialized in serialized_records]
    assert {item["stable_key"] for item in evidence} == {
        f"{runtime_key}/1",
        f"{judge_key}/1",
    }
    assert all(item["response"]["usage"]["total_tokens"] == 168 for item in evidence)
    assert not any(
        "credential" in key.casefold() for item in evidence for key in _all_mapping_keys(item)
    )
    for serialized in serialized_records:
        assert API_KEY not in serialized
        assert BASE_URL not in serialized
        assert "Authorization" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["runtime", "cancelled"])
async def test_provider_failure_keeps_unknown_reservation_and_halts(tmp_path, error_name):
    ledger = ResearchBudgetLedger(tmp_path / f"{error_name}.sqlite3")
    fake = FailingClient(error_name)
    meter = BudgetedAliyunClient(
        ledger=ledger,
        base_url=BASE_URL,
        private_dir=tmp_path / "private",
        client=fake,
    )
    scope_key = f"case/{CASE_ID}/judge"

    with meter.scope(scope_key, _metadata("judge")):
        if error_name == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await meter.acompletion(**_payload())
        else:
            with pytest.raises(
                BudgetedProviderError,
                match="PROVIDER_DISPATCH_UNCONFIRMED",
            ):
                await meter.acompletion(**_payload())

    assert fake.calls == 1
    summary = ledger.summary()
    assert summary["status_counts"][UNKNOWN_OUTCOME] == 1
    assert summary["held_reserved_cny"] == Decimal("5.242880")
    assert summary["actual_settled_cny"] == 0
    assert summary["halted"] is True
    assert len(meter.records) == 1
    attempt = meter.records[0]
    assert attempt["status"] == UNKNOWN_OUTCOME
    assert attempt["usage"] is None
    assert attempt["actual_cny"] is None
    assert attempt["estimated_cny"] == Decimal("5.242880")
    assert attempt["latency_ms"] >= 0

    with meter.scope(
        f"{scope_key}/next",
        _metadata("judge"),
    ):
        with pytest.raises(BudgetHalted):
            await meter.acompletion(**_payload())
    assert fake.calls == 1
    assert len(meter.records) == 1


@pytest.mark.asyncio
async def test_missing_scope_and_duplicate_key_never_dispatch(tmp_path):
    ledger = ResearchBudgetLedger(tmp_path / "guards.sqlite3")
    fake = RecordingClient(ledger)
    meter = BudgetedAliyunClient(
        ledger=ledger,
        base_url=BASE_URL,
        private_dir=tmp_path / "private",
        client=fake,
    )
    scope_key = f"case/{CASE_ID}/judge"
    metadata = _metadata("judge")

    with pytest.raises(BudgetedProviderError, match="PROVIDER_SCOPE_REQUIRED"):
        await meter.acompletion(**_payload())
    assert len(fake.calls) == 0
    assert ledger.summary()["dispatch_marks"] == 0
    assert meter.records == []

    with meter.scope(scope_key, metadata):
        await meter.acompletion(**_payload())
    assert len(fake.calls) == 1
    assert len(meter.records) == 1

    with meter.scope(scope_key, metadata):
        with pytest.raises(CallStateConflict, match="LOGICAL_CALL_ALREADY_EXISTS"):
            await meter.acompletion(**_payload())
    assert len(fake.calls) == 1
    assert len(meter.records) == 1
    assert ledger.summary()["dispatch_marks"] == 1
