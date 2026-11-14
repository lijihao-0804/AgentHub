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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"answer":"Check [1].","citation_ids":[2,9],', {"1": True, "2": True, "9": False}),
        ('{"answer":"Check [1].","citation_ids":[2,9]}', {"1": True, "2": True, "9": False}),
        ("A source with an ambiguous reference.", {}),
    ],
)
def test_raw_failed_citations_remain_reviewable(raw, expected):
    case = dict(input="Q", stratum="S", rubric=[], documents=[])
    record = dict(
        status="schema_failure",
        evidence=[{"number": 1}, {"number": 2}],
        model_events=[{"raw_message": {"content": raw}}],
    )
    card = score.case_card("citation", case, record)
    assert card["answer"] is None and card["raw_generation"] == raw
    assert card["cited_id_locatable"] == expected and card["citation_review_note"]
    assert score.win("citation", card, {}) == 0


def test_empty_choices_and_unknown_checkpoint_export_and_report(tmp_path, monkeypatch):
    root = tmp_path / "study"
    blind = tmp_path / "blind"
    save(tmp_path / "freeze.json", {"synthetic": True})
    monkeypatch.setattr(score, "OUT", tmp_path)
    monkeypatch.setattr(score, "verify", lambda: {})
    monkeypatch.setattr(runner, "verify", lambda: {})
    monkeypatch.setattr(runner, "identity", lambda: score.file_sha(tmp_path / "freeze.json"))
    case = dict(
        case_id="q",
        input="Q",
        stratum="S",
        rubric=[],
        documents=[],
        should_withhold=False,
        required_reads=[],
        decision="deny",
    )
    monkeypatch.setattr(
        score,
        "cases",
        lambda: [(typ, case, arm) for typ in ("citation", "support") for arm in score.ARMS],
    )
    runner.initialize(root, "synthetic")
    citation = "citation/q/baseline"
    support = "support/q/baseline"
    for key in (citation, support):
        (root / key).mkdir(parents=True)
    first = root / citation / "first.json"
    save(
        first,
        dict(
            task=citation,
            model_events=[{"raw_message": None}],
            status="gateway_error",
            terminal_known=True,
            physical_receipts=[],
        ),
    )
    save(
        root / support / "run.json",
        dict(
            case_id="q",
            arm="baseline",
            turn={"final_output": "POL-Q"},
            run={"status": "SUCCEEDED"},
            approvals_after=[dict(decision_status="DENIED", execution_status="NOT_STARTED")],
        ),
    )
    with closing(sqlite3.connect(root / "ledger.sqlite")) as db, db:
        db.execute(
            "INSERT INTO attempts VALUES (?, 'complete', ?)", (citation, score.file_sha(first))
        )
        db.execute("INSERT INTO attempts VALUES (?, 'active', NULL)", (support,))
    score.export(root, blind)
    private = score.load(blind / "private.json")["tasks"]
    cards = {private[row["token"]]: row for row in score.load(blind / "cards.json")}
    assert cards[citation]["raw_generation"] is None
    assert cards[support]["unconfirmed_turn_output"] == "POL-Q"
    assert cards[support]["answer"] is None and cards[support]["machine"]["run_status"] == "unknown"
    votes = {
        row["token"]: dict.fromkeys(score.AXES, True)
        | dict(reason="synthetic", citation_support=dict.fromkeys(row["cited_id_locatable"], False))
        for row in cards.values()
    }
    save(tmp_path / "votes.json", votes)
    score.report(root, blind, tmp_path / "votes.json", tmp_path / "report.json")
    result = score.load(tmp_path / "report.json")
    for chain in ("citation", "support"):
        group = result[chain]["arms"]["baseline"]
        assert group["dispatched"] == group["missing_answer"] == 1 and group["correct"] == 0
        assert result[chain]["pairs"][0]["arms"]["baseline"]["quality"] == 0
        assert result[chain]["pairs"][0]["arms"]["candidate"]["quality"] is None
    assert result["support"]["arms"]["baseline"]["completed"] == 0


@pytest.mark.asyncio
async def test_model_option_drift_before_dispatch(tmp_path, monkeypatch):
    root = tmp_path / "study"
    monkeypatch.setattr(runner, "verify", lambda: {})
    monkeypatch.setattr(runner, "identity", lambda: "frozen")
    runner.initialize(root, "local test")
    p = root / "ledger.sqlite"
    meter = runner.StudyMeter(p, "support")
    meter.task_id = "support/x/baseline"
    runner.claim(p, meter.task_id)
    with pytest.raises(ValueError, match="Provider options"):
        await meter.acompletion(
            extra_body={"enable_thinking": False}, temperature=0, max_tokens=2048
        )
    with pytest.raises(RuntimeError, match="stopped"):
        runner.claim(p, "citation/next/candidate")
    with closing(sqlite3.connect(p)) as db:
        assert db.execute("SELECT count(*) FROM wires").fetchone() == (0,)


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
