import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from benchmarks.public_rag import product_confirmation as runner
from benchmarks.public_rag.confirmation_meter import LIMITS, PER_REQUEST, AliyunMeter, init_ledger
from tests.unit.test_rag_confirmation_budget import payload


@pytest.mark.asyncio
async def test_selector_limit_before_wire_and_stage_partition(tmp_path):
    ledger = tmp_path / "study.sqlite"
    init_ledger(ledger, approval_id="synthetic", cap_microcny=PER_REQUEST * 2)
    gate = AliyunMeter(ledger, "selector")
    gate.task_id = "q/C"
    wire = AsyncMock()
    req = payload(wire)
    with pytest.raises(ValueError, match="output limit"):
        await gate.acompletion(**{**req, "max_tokens": 257})
    with closing(sqlite3.connect(ledger)) as db, db:
        db.executemany(
            "insert into wires(stage,task,status,cost) values('selector',?,'returned',1)",
            [(str(i),) for i in range(LIMITS["selector"][0])],
        )
    with pytest.raises(RuntimeError, match="limit"):
        await gate.acompletion(**req)
    wire.assert_not_awaited()
    other = AliyunMeter(ledger, "answer")
    other.task_id = "q/B"
    assert other.reserve() > 0


@pytest.mark.asyncio
async def test_missing_credentials_cannot_burn_opening(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_inputs", lambda _: [])

    def absent():
        raise ValueError("no key")

    monkeypatch.setattr(runner, "_credentials", absent)
    db = tmp_path / "synthetic.sqlite"
    init_ledger(db, approval_id="synthetic", cap_microcny=PER_REQUEST)
    with pytest.raises(ValueError, match="no key"):
        await runner.run(None, db, tmp_path / "events")
    assert not (tmp_path / "events").exists()
    with closing(sqlite3.connect(db)) as conn:
        assert not conn.execute(
            "select name from sqlite_master where name='study_opening'"
        ).fetchall()
