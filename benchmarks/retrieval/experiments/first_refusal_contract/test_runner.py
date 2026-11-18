"""Fail-closed accounting and private-archive eligibility for the paired QA experiment."""

import json
import sqlite3
from contextlib import closing

import pytest

from benchmarks.retrieval.experiments.first_refusal_contract import billing, campaign
from benchmarks.retrieval.experiments.output_limit import meter as previous
from benchmarks.retrieval.experiments.output_limit.report import read_ledger


def body():
    return {
        "id": "sample-unique-id",
        "model": previous.MODEL,
        "created": 1,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "answer"},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    }


def test_exact_wire_cost_is_paid_once_across_two_product_exchanges():
    raw = body()
    key = "paired/fresh/Q/2048/1/wire"
    ledger = {
        raw["id"]: dict(
            key=key,
            qid="Q",
            cap=2048,
            digest="msgs",
            cost=billing.charge(raw, 2048),
            response=json.dumps(raw),
        )
    }
    event = dict(
        id=raw["id"],
        created=1,
        content="answer",
        finish="stop",
        usage=raw["usage"],
        physical_key=key,
        messages_sha256="msgs",
        request_sha256="wire",
    )
    base = dict(id="fresh/Q", qid="Q", cap=2048, mode="live")
    rows = [
        dict(base, arm=arm, events=[dict(event, provenance=source)])
        for arm, source in (("baseline", "provider"), ("candidate", "confirmed_reuse"))
    ]
    result = billing.account(rows, {}, ledger)
    assert result["new_physical"] == 1 and result["new_confirmed_cached_uses"] == 1
    assert all(
        g["logical_model_cost_cny"] == result["new_cost_cny"]
        for g in result["logical_by_mode_arm"].values()
    )
    with pytest.raises(ValueError):
        billing.account([rows[1]], {}, ledger)
    with pytest.raises(ValueError):
        billing.account([rows[0], rows[0]], {}, ledger)
    with pytest.raises(ValueError):
        billing.account(
            [dict(rows[0], events=[dict(rows[0]["events"][0], request_sha256="changed")])],
            {},
            ledger,
        )


def test_unknown_duplicate_receipt_and_budget_halt_before_next_send(tmp_path, monkeypatch):
    monkeypatch.setattr(previous, "historical_ids", lambda: frozenset())
    m = object.__new__(billing.SuccessorMeter)
    previous.Meter.__init__(m, tmp_path / "meter.sqlite")
    m.prior_ids = {body()["id"]}
    with pytest.raises(ValueError, match="historically unresolved"):
        m.reserve("a", "2hop__75213_24779", [], 2048)
    assert m.reserve("a", "allowed", [], 2048) is None
    with pytest.raises(RuntimeError, match="duplicate historical"):
        m.settle("a", body())
    with pytest.raises(RuntimeError, match="unresolved"):
        m.reserve("b", "another", [], 2048)
    with pytest.raises(ValueError, match="Unsettled"):
        read_ledger(m.path)
    with closing(sqlite3.connect(m.path)) as db:
        assert db.execute("select count(*) from calls").fetchone() == (1,)
    tight = previous.Meter(tmp_path / "budget.sqlite")
    monkeypatch.setattr(previous, "CAP", previous.OPENING + 1)
    with pytest.raises(RuntimeError, match="exhausted"):
        tight.reserve("a", "allowed", [], 2048)


def test_eligibility_keeps_distinct_receipts_for_same_prompt():
    if not billing.OLD_LEDGER.exists():
        pytest.skip("Private historical receipts required")
    rows, old = billing.history()
    cases, high = campaign.register_cases(rows, fresh=False)
    archived = [c for c in cases if c["mode"] == "archive"]
    assert len(archived) == 335
    assert len({c["registered_wire"] for c in archived}) == 329
    ids = [high[c["archive_id"]]["events"][0]["id"] for c in archived]
    assert len(set(ids)) == len(ids)
    assert set(ids) <= old.keys()
    labels = campaign.lines(campaign.FOLDER.parent / "output_limit/results/row-status.jsonl")
    assert {c["archive_id"] for c in archived} == {
        r["id"]
        for r in labels
        if r["cap"] == 2048
        and (r["qid"] in campaign.TARGETS or r["first"]["strict"] or r["final"]["strict"])
    }
