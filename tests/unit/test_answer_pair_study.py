import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from benchmarks.public_rag import answer_pair_run as runner
from benchmarks.public_rag import answer_pair_score as score
from benchmarks.public_rag.confirmation_cases import save
from benchmarks.public_rag.confirmation_meter import PER_REQUEST


def test_single_open_unknown_wire_and_original_receipt(monkeypatch, tmp_path):
    root = tmp_path / "study"
    monkeypatch.setattr(runner, "verify", lambda: {})
    monkeypatch.setattr(runner, "identity", lambda: "frozen")
    runner.initialize(root, "test authorization")
    dbfile = root / "ledger.sqlite"
    key = "citation/q-1/baseline"
    runner.claim(dbfile, key)
    meter = runner.StudyMeter(dbfile, "citation")
    meter.task_id = key
    wire_id = meter.reserve(11)
    with pytest.raises(RuntimeError, match="Unknown"):
        runner.claim(dbfile, "citation/q-1/candidate")
    with pytest.raises(RuntimeError, match="Unsettled"):
        meter.reserve(11)
    with closing(sqlite3.connect(dbfile)) as db, db:
        db.execute(
            "UPDATE wires SET status='returned',input=11,output=2,cost=38,provider_id='id' "
            "WHERE id=?",
            (wire_id,),
        )
        entries = runner.wires(db, key)
    with pytest.raises(RuntimeError, match="task dispatch limit"):
        meter.reserve(11)
    with pytest.raises(RuntimeError, match="Unreconciled"):
        runner.claim(dbfile, "citation/q-1/candidate")
    receipt = tmp_path / "answer.json"
    save(
        receipt,
        {
            "task": key,
            "terminal_known": True,
            "physical_receipts": entries,
            "model_events": [{"id": "id", "usage": {"prompt_tokens": 11, "completion_tokens": 2}}],
        },
    )
    runner.finish(dbfile, key, receipt)
    with pytest.raises(sqlite3.IntegrityError):
        runner.claim(dbfile, key)
    runner.claim(dbfile, "citation/q-1/candidate")
    assert runner.CAP == 160 * PER_REQUEST == 13_107_200


def test_case_scoring_distinguishes_failures_and_writes():
    vote = dict.fromkeys(score.AXES, False)
    vote.update(
        correct_content=True,
        conditions_complete=True,
        visible_support=True,
        judgment_appropriate=True,
        contract_consistent=True,
        all_policy_claims_cited=True,
        citation_support={"source": True},
    )
    good = {
        "answer": "Supported.",
        "machine": {"run_status": "SUCCEEDED", "required_reads_met": True, "write_ok": True},
        "cited_id_locatable": {"source": True},
    }
    assert score.win("support", good, vote) == 1
    assert score.win("support", good, vote | {"unsupported_capability": True}) == 0
    assert (
        score.win("support", good | {"machine": good["machine"] | {"write_ok": False}}, vote) == 0
    )
    assert score.win("support", good | {"answer": None}, vote) == 0
    assert (
        score.win("citation", good | {"machine": {"run_status": "citations_on_refusal"}}, vote) == 0
    )


@pytest.mark.asyncio
async def test_model_option_drift_before_dispatch(tmp_path):
    p = tmp_path / "ledger"
    p.touch()
    meter = runner.StudyMeter(p, "support")
    with pytest.raises(ValueError, match="Provider options"):
        await meter.acompletion(
            extra_body={"enable_thinking": False}, temperature=0, max_tokens=2048
        )


@pytest.mark.asyncio
async def test_citation_both_arms_consume_one_request_with_same_evidence(monkeypatch):
    case = {"case_id": "q", "input": "Which colour is printed?"}
    contexts = {
        "q": {
            "visible": [
                dict(
                    number=1,
                    document_id="d",
                    document_revision_id="r",
                    chunk_id="c",
                    source="label",
                    locator={"line": 1},
                    text="The ink is navy.",
                    retrieval_score=1.0,
                    rerank_score=1.0,
                    metadata={},
                )
            ]
        }
    }
    seen = []

    async def generated(self, ctx, plan, request):
        seen.append(request)
        self.events.append({"id": "synthetic"})
        return SimpleNamespace(
            content='{"answer":"Navy [1].","insufficient_evidence":false,"citation_ids":[1]}',
            finish_reason="stop",
        )

    monkeypatch.setattr(runner.Gateway, "generate_resolved", generated)
    monkeypatch.setattr(runner, "text_counter", lambda: SimpleNamespace(count_serialized_text=len))
    meter = SimpleNamespace(path=None, stage="citation", task_id="synthetic")
    for arm in ("baseline", "candidate"):
        result = await runner.citation(case, arm, meter, contexts)
        assert result["answer"] == "Navy [1]." and result["terminal_known"]
    assert len(seen) == 2 and seen[0].messages[0] != seen[1].messages[0]
    assert seen[0].messages[1] == seen[1].messages[1]
