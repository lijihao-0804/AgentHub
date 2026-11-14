import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.public_rag.confirmation_meter import MODEL, PER_REQUEST, AliyunMeter, init_ledger

BASE = "https://test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


def payload(create):
    sdk = SimpleNamespace(
        base_url=BASE,
        max_retries=0,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    return dict(
        client=sdk,
        model="openai/" + MODEL,
        api_base=BASE,
        num_retries=0,
        messages=[dict(role="user", content="synthetic test")],
        max_tokens=256,
    )


def meter(tmp_path, cap=PER_REQUEST * 3):
    path = tmp_path / "ledger.sqlite3"
    init_ledger(path, approval_id="offline-test-only", cap_microcny=cap)
    instance = AliyunMeter(path, "refund_dev")
    instance.task_id = "test"
    return instance


@pytest.mark.asyncio
async def test_returned_usage_reconciled_and_task_limit_persists(tmp_path):
    m = meter(tmp_path)
    returned = SimpleNamespace(
        id="synthetic",
        model=MODEL,
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25),
    )
    request = payload(AsyncMock(return_value=returned))
    for _ in range(4):
        await m.acompletion(**request)
    with pytest.raises(RuntimeError, match="limit"):
        await m.acompletion(**request)
    with closing(sqlite3.connect(m.path)) as db:
        assert db.execute("SELECT count(*),sum(cost) FROM wires").fetchone() == (4, 320)
    assert request["client"].chat.completions.create.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "identity", "missing_usage", "usage_excess"])
async def test_unknown_or_unpriced_blocks_every_stage_and_retains_reservation(tmp_path, outcome):
    m = meter(tmp_path)
    usage = SimpleNamespace(prompt_tokens=15, completion_tokens=4, total_tokens=19)
    returned = SimpleNamespace(id="synthetic", model=MODEL, usage=usage)
    if outcome == "identity":
        returned.model = "unexpected"
    elif outcome == "missing_usage":
        returned.usage = None
    elif outcome == "usage_excess":
        usage.completion_tokens = 10000
    wire = AsyncMock(
        return_value=returned, side_effect=TimeoutError() if outcome == "timeout" else None
    )
    with pytest.raises(RuntimeError):
        await m.acompletion(**payload(wire))
    other = AliyunMeter(m.path, "answer")
    other.task_id = "technical"
    with pytest.raises(RuntimeError, match="Unsettled"):
        await other.acompletion(**payload(wire))
    with closing(sqlite3.connect(m.path)) as db:
        assert db.execute("SELECT status,cost FROM wires").fetchone() == ("unsettled", PER_REQUEST)
    assert wire.await_count == 1


@pytest.mark.asyncio
async def test_oversize_bad_route_and_insufficient_cap_reject_before_egress(tmp_path):
    m = meter(tmp_path, PER_REQUEST - 1)
    wire = AsyncMock()
    request = payload(wire)
    with pytest.raises(ValueError, match="Route"):
        await m.acompletion(**{**request, "api_base": "https://example.test"})
    with pytest.raises(ValueError, match="input"):
        await m.acompletion(**{**request, "messages": [dict(role="user", content="好 " * 40000)]})
    with pytest.raises(RuntimeError, match="budget"):
        await m.acompletion(**request)
    with pytest.raises(FileExistsError):
        init_ledger(m.path, approval_id="duplicate", cap_microcny=PER_REQUEST)
    wire.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_history_estimate_records_separate_local_and_actual_counts(tmp_path):
    from benchmarks.public_rag.confirmation_meter import TOOL_TEMPLATE_ALLOWANCE, input_bound

    m = meter(tmp_path)
    returned = SimpleNamespace(
        id="simulated",
        model=MODEL,
        usage=SimpleNamespace(prompt_tokens=2000, completion_tokens=4, total_tokens=2004),
    )
    create = AsyncMock(return_value=returned)
    request = payload(create)
    request.update(
        messages=[
            {"role": "user", "content": "查看政策"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "policy", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "a",
                "content": "该产品的退款申请必须附上签收单。" * 800,
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "policy",
                    "description": "检索政策",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    fields = ("messages", "tools", "max_tokens")
    full = {k: request[k] for k in fields}
    estimate = input_bound(full)
    assert len(request["messages"][-1]["content"].encode()) > 32768
    assert TOOL_TEMPLATE_ALLOWANCE < estimate < 32768
    assert estimate > input_bound({**full, "tools": []})
    await m.acompletion(**request)
    with closing(sqlite3.connect(m.path)) as db:
        assert db.execute(
            "select local_input_estimate,input,output,cost from wires"
        ).fetchone() == (estimate, 2000, 4, 4032)
    create.assert_awaited_once()
