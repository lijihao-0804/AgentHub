"""Synthetic-only checks of the one-shot S0-038 continuation gates."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from benchmarks.evaluation.experiments.support_refund_rag import product_continuation as cont
from benchmarks.public_rag.confirmation_meter import PER_REQUEST, AliyunMeter, init_ledger


@pytest.fixture
def prefix(tmp_path):
    path = tmp_path / "study.sqlite"
    init_ledger(path, approval_id="fixture", cap_microcny=100 * PER_REQUEST)
    cont.acceptance.open_paid_schedule(path)
    records = []
    with closing(sqlite3.connect(path)) as db, db:
        db.row_factory = sqlite3.Row
        for key in cont.PREFIX:
            db.execute(
                "INSERT INTO wires(stage,task,status,input,output,cost,provider_id) "
                "VALUES ('refund_dev',?,'returned',1,1,10,?)",
                (key, key),
            )
            receipt = dict(db.execute("SELECT * FROM wires WHERE task=?", (key,)).fetchone())
            records.append({"case_id": key, "physical_attempts": [receipt]})
    return path, records


def test_concurrent_claim_owns_only_tail(prefix):
    path, records = prefix
    cont.ledger_gate(path, records)

    def claim():
        try:
            cont.ledger_gate(path, records, claim=True)
        except ValueError as exc:
            assert "already claimed" in str(exc)
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]
    with pytest.raises(ValueError, match="already claimed"):
        cont.ledger_gate(path, records, claim=True)
    with closing(sqlite3.connect(path)) as db:
        assert set(db.execute("SELECT name FROM study_opening")) == {
            ("refund_dev_and_new",),
            (cont.CLAIM,),
        }
        assert db.execute("SELECT count(*) FROM wires").fetchone()[0] == 38


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("DELETE FROM study_opening", "opening missing"),
        ("UPDATE wires SET provider_id='other' WHERE task='S0-038'", "differs from original"),
        ("UPDATE wires SET status='unsettled' WHERE task='S0-001'", "Unsettled"),
        (
            "INSERT INTO wires(stage,task,status) VALUES ('answer','external','unsettled')",
            "Unsettled",
        ),
        (
            "INSERT INTO wires(stage,task,status) VALUES ('refund_new','NEW-LOCKER','returned')",
            "already dispatched",
        ),
        (
            "INSERT INTO wires(stage,task,status) VALUES ('refund_dev','S0-039','returned')",
            "already dispatched",
        ),
    ],
)
def test_claim_fails_closed(prefix, mutation, match):
    path, records = prefix
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(mutation)
    with pytest.raises(ValueError, match=match):
        cont.ledger_gate(path, records, claim=True)
    with closing(sqlite3.connect(path)) as db:
        assert not db.execute(
            "SELECT name FROM study_opening WHERE name=?", (cont.CLAIM,)
        ).fetchone()


def test_prefix_reordered_or_too_many_wires(prefix):
    path, records = prefix
    with pytest.raises(ValueError, match="Incorrect original prefix"):
        cont.ledger_gate(path, records[::-1], claim=True)
    records[-1]["physical_attempts"] *= 5
    with pytest.raises(ValueError, match="Unexpected original physical"):
        cont.ledger_gate(path, records, claim=True)


def test_tail_uses_original_four_wire_limit(prefix):
    path, records = prefix
    cont.ledger_gate(path, records, claim=True)
    meter = AliyunMeter(path, "refund_dev")
    meter.task_id = "S0-039"
    for i in range(4):
        wire = meter.reserve()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE wires SET status='returned',input=1,output=1,cost=10,"
                "provider_id=? WHERE id=?",
                (str(i), wire),
            )
    with pytest.raises(RuntimeError, match="dispatch limit"):
        meter.reserve()


@pytest.mark.asyncio
async def test_refresh_checks_authoritative_principal(monkeypatch):
    owner, operator = AsyncMock(), AsyncMock()
    owner.headers, operator.headers = {}, {}
    auth = AsyncMock(
        side_effect=[
            {"user_id": "owner", "access_token": "a"},
            {"user_id": "wrong", "access_token": "b"},
        ]
    )
    monkeypatch.setattr(cont.acceptance, "api", auth)
    with pytest.raises(ValueError, match="principal"):
        await cont.acceptance.refresh_roles(owner, operator, ["owner", "operator"])
    assert owner.headers == {"Authorization": "Bearer a"}
    assert operator.headers == {}
    assert all(call.args[1:] == ("POST", "auth/refresh") for call in auth.await_args_list)
