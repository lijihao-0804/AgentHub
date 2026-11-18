from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from benchmarks.retrieval.experiments.output_limit import meter


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(meter, "historical_ids", lambda: {"old"})
    return meter.Meter(tmp_path / "wire.sqlite")


def receipt(hour=0):
    return {
        "model": meter.MODEL,
        "id": "new",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}
        ],
        "created": int(datetime(2026, 11, 19, hour, tzinfo=UTC).timestamp()),
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 50},
            "completion_tokens_details": {},
        },
    }


@pytest.mark.parametrize(("hour", "cost"), [(0, 26200), (14, 13100)])
def test_confirmed_replay(ledger, hour, cost):
    raw = receipt(hour)
    raw["choices"][0]["message"]["reasoning_content"] = "excluded"
    send = Mock(return_value=raw)
    for _ in range(2):
        assert ledger.run("first", "Q", [], 256, send) == receipt(hour)
    send.assert_called_once()
    with ledger.connection() as db:
        assert db.execute("SELECT cost FROM calls").fetchone()[0] == cost


@pytest.mark.parametrize("response", [{"id": "new"}, {"id": "old"}])
def test_unknown_blocks_all_calls(ledger, response):
    with pytest.raises((RuntimeError, KeyError)):
        ledger.run("first", "Q", [], 256, lambda: response)
    send = Mock()
    with pytest.raises(RuntimeError, match="NEEDS_ATTENTION"):
        ledger.run("second", "R", [], 512, send)
    send.assert_not_called()


@pytest.mark.parametrize("field", ["choices", "role", "content"])
def test_invalid_answer_charged_and_halted_across_restart(ledger, field):
    body = receipt()
    if field == "choices":
        del body["choices"]
    else:
        del body["choices"][0]["message"][field]
    with pytest.raises(RuntimeError, match="confirmed usage with invalid answer"):
        ledger.run("first", "Q", [], 256, lambda: body)
    with ledger.connection() as db:
        assert db.execute("SELECT cost, response_id FROM calls").fetchone() == (26200, "new")
    restarted = meter.Meter(ledger.path)
    send = Mock()
    for key in ("first", "second"):
        with pytest.raises(RuntimeError, match="confirmed usage with invalid answer"):
            restarted.run(key, "Q", [], 256, send)
    send.assert_not_called()
