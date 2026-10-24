import json
from types import SimpleNamespace

import pytest

from benchmarks.public_rag import consumer_answers


def _setup(folder, status):
    prepared, prior = folder / "prepared", folder / "prior"
    prepared.mkdir()
    prior.mkdir()
    protocol = {"seed": 1, "answers": {"model": "deepseek-v4.1-flash-1017", "workers": 2}}
    protocol_path = folder / "protocol.json"
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    meta = {
        "protocol": protocol,
        "profiles": ["legacy5"],
        "queries": 2,
        "dataset": "musique",
        "split": "train",
        "prepared": str(prepared),
    }
    for path in (prepared / "meta.json", prior / "meta.json"):
        path.write_text(json.dumps(meta), encoding="utf-8")
    (prepared / "completed.json").write_text(json.dumps({"expected": 2}), encoding="utf-8")
    arm = {
        "admission_verified": True,
        "budget": {"estimated_input_after": 1, "available_input": 9},
        "messages": [{"role": "user", "content": "second"}],
        "admitted_chunk_ids": [],
        "final_evidence_bytes": 0,
        "stages": {
            "final_admitted_context": {
                "best_evidence_recall": 0,
                "complete_evidence_set": 0,
                "full_text_evidence_recall": 0,
            }
        },
    }
    rows = [
        {"query_id": q, "group_id": q, "hops": 2, "answers": ["2"], "arms": {"legacy5": arm}}
        for q in ("first", "second")
    ]
    (prepared / "prepared.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    api = {"status": status, "usage": None, "request_ms": 1}
    if status == "PROVIDER_ERROR":
        api["http_status"] = 400
    (prior / "api-events.jsonl").write_text(
        json.dumps({"query_id": "first", "profile": "legacy5", "api": api}), encoding="utf-8"
    )
    return SimpleNamespace(
        prepared=prepared, continue_from=prior, protocol=protocol_path, output=folder / "new"
    )


@pytest.mark.asyncio
async def test_continuation_never_reissues_a_known_rejection_and_keeps_its_zero(
    tmp_path, monkeypatch
):
    args = _setup(tmp_path, "PROVIDER_ERROR")
    calls = []

    async def generate(messages, **_kwargs):
        calls.append(messages)
        return {
            "status": "VALID",
            "usage": None,
            "request_ms": 1,
            "answer": "2",
            "unanswerable": False,
            "cited_chunk_ids": [],
        }

    monkeypatch.setattr(consumer_answers, "generate_once", generate)
    monkeypatch.setattr(
        consumer_answers, "_credentials", lambda: ("https://test.invalid/chat/completions", "dummy")
    )
    await consumer_answers.run(args)
    assert len(calls) == 1
    rows = [json.loads(line) for line in (args.output / "run.jsonl").read_text().splitlines()]
    assert rows[0]["arms"]["legacy5"]["api"]["http_status"] == 400
    assert rows[0]["arms"]["legacy5"]["token_f1"] == 0
    assert rows[1]["arms"]["legacy5"]["token_f1"] == 1
    done = json.loads((args.output / "completed.json").read_text())
    assert done["observed_calls"] == 2 and done["new_calls"] == 1


@pytest.mark.asyncio
async def test_unknown_outcome_blocks_continuation_before_any_new_request(tmp_path, monkeypatch):
    args = _setup(tmp_path, "UNKNOWN_OUTCOME")
    calls = []

    async def generate(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(consumer_answers, "generate_once", generate)
    with pytest.raises(ValueError, match="unknown"):
        await consumer_answers.run(args)
    assert calls == []
    assert not args.output.exists()


@pytest.mark.asyncio
async def test_incomplete_chained_ledger_cannot_reissue_earlier_requests(tmp_path):
    args = _setup(tmp_path, "PROVIDER_ERROR")
    meta_path = args.continue_from / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["prior_run"] = "an-earlier-ledger-not-merged-here"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="chained continuation"):
        await consumer_answers.run(args)
    assert not args.output.exists()
