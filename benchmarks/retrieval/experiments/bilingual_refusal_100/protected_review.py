"""Verify and review changed English control responses against their original sources."""

import argparse
import json
from pathlib import Path

import httpx

from benchmarks.retrieval.experiments.first_refusal_100 import attempts
from benchmarks.retrieval.experiments.first_refusal_100.dataset import load_documents
from benchmarks.retrieval.experiments.first_refusal_100.language_audit import language_match

from .history import OLD, previous, reconcile_history
from .review import dump, private, read_json, rows, validate_verdict


def materials(run, group):
    private(run)
    cases, _, _ = previous(group)
    freeze = read_json(run / "freeze.json")
    if freeze["offline_replay"] or freeze["group"] != group:
        raise ValueError("Expected the completed paid control run")
    if read_json(run / "execution.json") != reconcile_history(run, cases, freeze):
        raise ValueError("Control run changed after recording")
    wire = {r["key"]: r for r in rows(run / "wire-private.jsonl") if r["attempt"] == 1}
    http = {r["key"]: r for r in rows(run / "http-private.jsonl") if r["state"] == "returned"}
    if group == "old100":
        documents = load_documents()
    packets, observed = [], {}
    for case in cases:
        key = case["id"] + "/candidate"
        reply = http[key]
        evidence = reply["evidence"]
        doc_id = case["doc_id"]
        first = attempts.read_attempt(wire[key], evidence)
        final = attempts.read_http(
            httpx.Response(reply["http"], json=reply["body"]), freeze["inputs"][doc_id], doc_id
        )
        visible = [{"id": e["id"], "text": e["text"], "locator": e["locator"]} for e in evidence]
        units = {}
        for stage, result in (("first", first), ("final", final)):
            uid = None
            if result["state"] in {"refusal", "answer"}:
                unit = {
                    "state": result["state"],
                    "answer": result["answer"],
                    "citations": [{"id": c["id"], "text": c["text"]} for c in result["citations"]],
                }
                uid = attempts.digest_json([case["id"], visible, unit])[:24]
                units[uid] = {"uid": uid, **unit}
            observed[case["id"], stage] = (case["answerable"], result["state"], uid)
        packets.append(
            {
                "case": case,
                "visible_evidence": visible,
                "source": documents[doc_id]["text"]
                if group == "old100"
                else "The evidence contains the source cells and row locators of two W3C tables.",
                "outputs": [units[k] for k in sorted(units)],
            }
        )
    return sorted(packets, key=lambda p: attempts.digest_json(p["case"]["id"])), observed


def original(group):
    if group == "wai14":
        # Ten source-supported positives and four clean refusals in the previous candidate.
        return {(f"W{i:02}", s): True for i in range(1, 15) for s in ("first", "final")}
    from .history import ARCHIVE

    wire = {
        e["key"]: e for e in rows(ARCHIVE / "independent/wire-private.jsonl") if e["attempt"] == 1
    }
    http = {
        e["key"]: e
        for e in rows(ARCHIVE / "independent/http-private.jsonl")
        if e["state"] == "returned"
    }
    out = {}
    for r in rows(OLD / "results/reviewed.jsonl"):
        key = r["id"] + "/" + r["arm"]
        for s in ("first", "final"):
            if not r[s]["scores"]["joint_correct"]:
                continue
            text = (
                json.loads(wire[key]["content"])["answer"]
                if s == "first"
                else http[key]["body"]["answer"]
            )
            if language_match(text):
                out[r["id"], s] = True
    return out


def evaluate(run, group, dest, output):
    packets, observed = materials(run, group)
    private(dest)
    if output is None:
        dest.mkdir(parents=True, exist_ok=False)
        for n in (1, 2):
            dump(dest / f"packet-{n}.json", packets[n - 1 :: 2])
        return
    decisions = {}
    for n in (1, 2):
        packet = packets[n - 1 :: 2]
        if packet != read_json(dest / f"packet-{n}.json"):
            raise ValueError("Review packet changed")
        votes = read_json(dest / f"verdicts-{n}.json")
        if set(votes) != {v["uid"] for p in packet for v in p["outputs"]}:
            raise ValueError("Incomplete control review")
        for vote in votes.values():
            validate_verdict(vote, final=True)
        decisions.update(votes)
    prior = original(group)
    stages = {}
    public = []
    for stage in ("first", "final"):
        gained, lost, unresolved = [], [], []
        counts = {
            "strict": 0,
            "refusal": 0,
            "supported_positive": 0,
            "language": 0,
            "valid": 0,
            "supported_cited": 0,
            "cited": 0,
        }
        for (qid, s), (answerable, state, uid) in sorted(observed.items()):
            if s != stage:
                continue
            vote = decisions.get(uid, {})
            ok = bool(
                uid
                and answerable == (state == "answer")
                and all(vote[k] for k in ("value", "support", "language_match"))
            )
            if prior.get((qid, stage), False) != ok:
                (gained if ok else lost).append(qid)
            counts["strict"] += ok
            counts["refusal"] += ok and not answerable
            counts["supported_positive"] += ok and answerable
            counts["valid"] += uid is not None
            counts["language"] += bool(vote.get("language_match"))
            counts["cited"] += state == "answer"
            counts["supported_cited"] += state == "answer" and vote.get("support") is True
            if vote.get("unresolved"):
                unresolved.append(qid)
            public.append(
                {
                    "id": qid,
                    "stage": s,
                    "state": state,
                    "current": ok,
                    "previous_success": prior.get((qid, s), False),
                    "review": {k: vote.get(k) for k in ("value", "support", "language_match")},
                }
            )
        stages[stage] = {**counts, "gained": gained, "regressed": lost, "unresolved": unresolved}
    dump(
        output,
        {
            "questions": len(packets),
            "previous": "union of previously successful arms, including language",
            "answerable": sum(p["case"]["answerable"] for p in packets),
            "stages": stages,
            "rows": public,
        },
    )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--run", required=True, type=Path)
    cli.add_argument("--group", required=True, choices=("old100", "wai14"))
    cli.add_argument("--review", required=True, type=Path)
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    evaluate(args.run, args.group, args.review, args.output)
