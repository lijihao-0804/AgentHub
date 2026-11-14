import json
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from benchmarks.public_rag import confirmation_scoring as score
from packages.knowledge.citation_qa import _evidence_prompt, _NumberedEvidence
from packages.knowledge.contracts import RetrievedEvidence


def setup_study(tmp_path):
    args = SimpleNamespace(
        freeze=tmp_path,
        pools=tmp_path / "pools.json",
        events=tmp_path / "events.jsonl",
        out=tmp_path / "blind",
        first=tmp_path / "judgments1",
        second=tmp_path / "judgments2",
        ledger=tmp_path / "ledger",
        adjudicated=None,
    )
    case = {
        "id": "q",
        "query": "What is the value?",
        "negative": True,
        "gold_documents": ["p"],
        "answers": [],
        "evidence": [],
    }
    score.write(tmp_path / "cohort.json", {"cases": [case]})
    score.write(
        tmp_path / "source-review-a.json",
        [{"id": "q", "ruling": "answerable", "expected": "14", "quotes": ["value = 14"]}],
    )
    score.write(tmp_path / "source-review-b.json", [])
    chunk = RetrievedEvidence("p", "r", "c", "paper", {"char_start": 0}, "value = 14", 1.0, 2.0, {})
    score.write(args.pools, {"cases": [{"id": "q", "pool": [asdict(chunk)]}]})
    prompt = _evidence_prompt(case["query"], (_NumberedEvidence(1, chunk, chunk.text),))
    records = [
        {"dispatch_scheduled": "q/B"},
        {
            "task": "q/B",
            "id": "q",
            "arm": "B",
            "status": "ok",
            "total_ms": 510,
            "events": [{"stage": "answer", "messages": [{"role": "user", "content": prompt}]}],
            "visible": [{"number": 1, **asdict(chunk)}],
            "answer": "14 [1].",
            "citations": [1],
            "insufficient": False,
        },
        {"dispatch_scheduled": "q/C"},
    ]
    args.events.write_text("\n".join(json.dumps(r) for r in records), encoding="utf8")
    with sqlite3.connect(args.ledger) as db:
        db.executescript("""
        create table wires(id integer,stage text,task text,status text,input integer,
                           output integer,cost integer,provider_id text);
        insert into wires values(1,'answer','q/B','returned',100,10,280,'r1');
        insert into wires values(2,'selector','q/C','unsettled',null,null,81920,null);
        """)
    return args


def review(args, who, disagree=False):
    cards = score.read(args.out / f"{who}.json")
    vote = {k: False for k in score.FIELDS}
    vote.update(
        correct_content=not disagree,
        conditions_complete=True,
        critical_claims_supported=True,
        visible_sufficient=True,
        citation_support={"1": True},
    )
    return [
        {"token": c["token"], "judgment": vote, "reason": "The cited value is 14."} for c in cards
    ]


def test_blind_report_adjudication_and_interruption(tmp_path):
    args = setup_study(tmp_path)
    score.export(args)
    first = score.read(args.out / "first.json")
    second = score.read(args.out / "second.json")
    assert first[0]["token"] != second[0]["token"]
    assert set(first[0]) == {
        "question",
        "official_negative",
        "answers",
        "gold_evidence",
        "source_review",
        "answer",
        "insufficient",
        "citation_ids",
        "visible",
        "token",
    }
    assert set(first[0]["visible"][0]) == {"number", "source", "locator", "text"}
    score.write(args.first, review(args, "first"))
    score.write(args.second, review(args, "second", disagree=True))
    assert score.report(args) is None
    disputed = score.read(args.out / "disagreements.json")
    assert len(disputed) == 1 and "arm" not in disputed[0]
    args.adjudicated = tmp_path / "final"
    score.write(args.adjudicated, review(args, "first"))
    res = score.report(args)
    assert res["arms"]["B"]["official"]["success"] == 0
    assert res["arms"]["B"]["source"]["success"] == 1
    assert res["arms"]["B"]["locatable_count"] == 1
    assert res["arms"]["C"]["status_counts"] == {"unknown_outcome": 1}
    assert res["arms"]["D"]["source"]["scored"] == 0
    assert res["paired"]["C-B/source"]["loss"] == 1
    assert res["paired"]["D-B/source"]["paired_source_ci95"] is None
    assert res["ledger"]["C/selector"]["unsettled_reserved_microcny"] == 81920
    assert res["ledger"]["B/answer"]["estimated_microcny"] == 280


def test_reject_changed_card_or_prompt(tmp_path):
    args = setup_study(tmp_path)
    args.events.write_text(args.events.read_text().replace("value = 14", "value = 15", 1))
    with pytest.raises(ValueError, match="actual final request"):
        score.export(args)


def test_paired_cluster_and_degenerate_ci():
    def row(source, b, d):
        return {"source": source, "arms": {"B": {"source": b}, "D": {"source": d}}}

    data = [row("p", 1, 0), row("p", 1, 0), row("q", 0, 1)]
    result = score.paired(data, "D", "B", "source")
    assert (result["n"], result["sources"], result["loss"], result["win"]) == (3, 2, 2, 1)
    assert result["delta"] == pytest.approx(-1 / 3)
    assert result["paired_source_ci95"] == [-1, 1]


def test_success_and_refusal_require_consistent_visible_support():
    event = {"status": "ok", "insufficient": False}
    vote = dict.fromkeys(score.FIELDS, True)
    vote.update(
        unsupported_assertion=False,
        condition_omission=False,
        visible_sufficient=False,
        citation_support={"1": True},
    )
    assert score.quality(event, "answerable", vote) == 0
    event["insufficient"] = True
    vote["visible_sufficient"] = True
    assert score.quality(event, "unanswerable", vote) == 0
