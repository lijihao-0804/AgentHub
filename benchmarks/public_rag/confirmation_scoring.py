"""Blind two-reviewer export and paired reporting for the frozen public comparison."""

import argparse
import hashlib
import json
import random
import secrets
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from itertools import combinations
from pathlib import Path

from benchmarks.public_rag.confirmation_cases import OUT, read
from packages.knowledge.citation_qa import _evidence_prompt, _NumberedEvidence
from packages.knowledge.contracts import RetrievedEvidence

ARMS = ("B", "C", "D")
FIELDS = (
    "correct_content",
    "conditions_complete",
    "critical_claims_supported",
    "unsupported_assertion",
    "condition_omission",
    "refusal_justified",
    "visible_sufficient",
)


def digest(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def write(path, obj):
    with path.open("x", encoding="utf8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def indexed(rows, key):
    result = {r[key]: r for r in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate {key}")
    return result


def inputs(args):
    cases = indexed(read(args.freeze / "cohort.json")["cases"], "id")
    reviews = indexed(
        [r for name in ("a", "b") for r in read(args.freeze / f"source-review-{name}.json")], "id"
    )
    if set(reviews) != {k for k, c in cases.items() if c["negative"]}:
        raise ValueError("Incomplete pre-output source review")
    if any(
        r["ruling"] not in {"answerable", "unanswerable", "ambiguous"} for r in reviews.values()
    ):
        raise ValueError("Invalid source ruling")
    pools = indexed(read(args.pools)["cases"], "id")
    if set(pools) != set(cases):
        raise ValueError("Different cohort and pool")
    events, dispatch = {}, set()
    for line in args.events.read_text(encoding="utf8").splitlines():
        r = json.loads(line)
        if "dispatch_scheduled" in r:
            task = r["dispatch_scheduled"]
            if task in dispatch:
                raise ValueError("Repeated dispatch")
            dispatch.add(task)
        else:
            task = r["task"]
            if task not in dispatch or task in events or task != f"{r['id']}/{r['arm']}":
                raise ValueError("Unbound or duplicate outcome")
            events[task] = r
    tasks = {f"{k}/{arm}" for k in cases for arm in ARMS}
    if not dispatch <= tasks:
        raise ValueError("Unknown dispatch")
    for t in tasks - events.keys():
        events[t] = {"status": "unknown_outcome" if t in dispatch else "not_run"}
    return cases, reviews, pools, events


def blind_item(case, review, event):
    selected = [
        _NumberedEvidence(
            v["number"],
            RetrievedEvidence(**{k: x for k, x in v.items() if k != "number"}),
            v["text"],
        )
        for v in event["visible"]
    ]
    requests = [e for e in event["events"] if e["stage"] == "answer"]
    if selected and (
        len(requests) != 1
        or requests[0]["messages"][-1]
        != {"role": "user", "content": _evidence_prompt(case["query"], tuple(selected))}
    ):
        raise ValueError("Visible evidence differs from the actual final request")
    # Allowlist fields. No task IDs, arm, timings, retrieval scores, prompts or wire metadata.
    return {
        "question": case["query"],
        "official_negative": case["negative"],
        "answers": case["answers"],
        "gold_evidence": case["evidence"],
        "source_review": {k: review[k] for k in ("ruling", "expected", "quotes")}
        if review
        else None,
        "answer": event["answer"],
        "insufficient": event["insufficient"],
        "citation_ids": event["citations"],
        "visible": [
            {k: row[k] for k in ("number", "source", "locator", "text")} for row in event["visible"]
        ],
    }


def shuffled(items):
    values = list(items)
    secrets.SystemRandom().shuffle(values)
    return values


def export(args):
    cases, reviews, pools, events = inputs(args)
    args.out.mkdir(parents=True, exist_ok=False)
    custody = {"inputs": digest([cases, reviews, pools, events]), "reviewers": {}}
    for who in ("first", "second"):
        tokens, cards = {}, []
        for task in shuffled(t for t, e in events.items() if e["status"] == "ok"):
            q = task.rsplit("/", 1)[0]
            token = secrets.token_hex(16)
            card = blind_item(cases[q], reviews.get(q), events[task])
            tokens[token] = {"task": task, "content_sha256": digest(card)}
            cards.append({"token": token, **card})
        write(args.out / f"{who}.json", cards)
        custody["reviewers"][who] = tokens
    write(args.out / "private.json", custody)
    write(args.out / "commitment.json", {"private_sha256": digest(custody)})
    return custody


def checked_reviews(path, tokens, events):
    rows = indexed(read(path), "token")
    if rows.keys() != tokens.keys():
        raise ValueError("Missing, extra or cross-reviewer token")
    results = {}
    for token, row in rows.items():
        task = tokens[token]["task"]
        vote = row["judgment"]
        if not all(type(vote.get(k)) is bool for k in FIELDS):
            raise ValueError("Missing boolean judgment")
        declared = {str(i) for i in events[task]["citations"]}
        supports = vote.get("citation_support")
        if not isinstance(supports, dict):
            raise ValueError("Missing citation support map")
        if set(supports) != declared or any(type(v) is not bool for v in supports.values()):
            raise ValueError("Each cited ID requires one support judgment")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Missing evidence rationale")
        results[task] = {k: vote[k] for k in FIELDS} | {"citation_support": supports}
    return results


def lab(case, review, axis):
    if axis == "source" and review:
        return review["ruling"]
    return "unanswerable" if case["negative"] else "answerable"


def quality(event, label, vote):
    if label == "ambiguous" or event["status"] == "not_run":
        return None
    if event["status"] != "ok":
        return 0
    if event["insufficient"]:
        return int(
            label == "unanswerable"
            and vote["refusal_justified"]
            and not vote["visible_sufficient"]
            and not vote["unsupported_assertion"]
        )
    return int(
        label == "answerable"
        and vote["visible_sufficient"]
        and all(vote[k] for k in FIELDS[:3])
        and not vote["unsupported_assertion"]
        and not vote["condition_omission"]
        and bool(vote["citation_support"])
        and all(vote["citation_support"].values())
    )


def evidence(case, rows):
    rows = [r for r in rows if r["document_id"] in case["gold_documents"]]
    texts = [" ".join(r["text"].lower().split()) for r in rows]

    def hit(refs):
        if not refs:
            return None
        return any(" ".join(s.lower().split()) in t for s in refs if s.strip() for t in texts)

    return {
        "gold_document": bool(rows),
        "any_gold_passage_in_one_chunk": hit(case["evidence"]),
        "any_answer_string": hit(case["answers"]),
    }


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    i = (len(values) - 1) * p
    j = int(i)
    return values[j] + (i - j) * (values[min(j + 1, len(values) - 1)] - values[j])


def paired(rows, left, right, axis):
    groups = defaultdict(list)
    for r in rows:
        a, b = r["arms"][left][axis], r["arms"][right][axis]
        if a is not None and b is not None:
            groups[r["source"]].append(a - b)
    ds = [d for vals in groups.values() for d in vals]
    counts = Counter(ds)
    ci = None
    if groups:
        rng = random.Random(20261114)
        chunks = [(sum(vals), len(vals)) for vals in groups.values()]
        sims = []
        for _ in range(10000):
            sample = rng.choices(chunks, k=len(chunks))
            sims.append(sum(x for x, n in sample) / sum(n for x, n in sample))
        ci = [percentile(sims, 0.025), percentile(sims, 0.975)]
    return {
        "n": len(ds),
        "sources": len(groups),
        "win": counts[1],
        "loss": counts[-1],
        "tie": counts[0],
        "delta": sum(ds) / len(ds) if ds else None,
        "paired_source_ci95": ci,
    }


def billing(path, events):
    result = {}
    confirmed = set()
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT stage,task,status,input,output,cost,provider_id FROM wires "
            "WHERE stage IN ('answer','selector') ORDER BY id"
        ).fetchall()
    for stage, task, status, inp, out, cost, provider_id in rows:
        if task not in events or events[task]["status"] == "not_run":
            raise ValueError("Unscheduled ledger reservation")
        key = task.rsplit("/", 1)[1] + "/" + stage
        group = result.setdefault(key, Counter())
        group["reservation_count"] += 1
        if status == "returned":
            confirmed.add((task, stage, provider_id))
            group["confirmed_physical_responses"] += 1
            group["input_tokens"] += inp
            group["output_tokens"] += out
            group["estimated_microcny"] += cost
        else:
            group["unknown_dispatch_or_response"] += 1
            group["unsettled_reserved_microcny"] += cost
    if any(
        (task, event["stage"], event["id"]) not in confirmed
        for task, record in events.items()
        for event in record.get("events", [])
        if event.get("id")
    ):
        raise ValueError("Provider response missing from settled physical ledger")
    return result


def report(args):
    cases, reviews, pools, events = inputs(args)
    private = read(args.out / "private.json")
    if digest(private) != read(args.out / "commitment.json")["private_sha256"] or private[
        "inputs"
    ] != digest([cases, reviews, pools, events]):
        raise ValueError("Study inputs or custody changed since the blind export")
    votes = []
    for who in ("first", "second"):
        cards = indexed(read(args.out / f"{who}.json"), "token")
        tokens = private["reviewers"][who]
        if tokens.keys() != cards.keys() or any(
            digest({k: v for k, v in cards[t].items() if k != "token"})
            != tokens[t]["content_sha256"]
            for t in tokens
        ):
            raise ValueError("Blind packet changed")
        votes.append(checked_reviews(getattr(args, who), tokens, events))
    conflicts = {t for t in votes[0] if votes[0][t] != votes[1][t]}
    tokens = {k: v for k, v in private["reviewers"]["first"].items() if v["task"] in conflicts}
    if conflicts and not args.adjudicated:
        # No arm is revealed to the adjudicator. The two anonymous positions are shown.
        cards = indexed(read(args.out / "first.json"), "token")
        write(
            args.out / "disagreements.json",
            [
                {**cards[t], "anonymous_judgments": [v[tokens[t]["task"]] for v in votes]}
                for t in tokens
            ],
        )
        return None
    final = dict(votes[0])
    if args.adjudicated:
        final.update(checked_reviews(args.adjudicated, tokens, events))
    rows, by_arm = [], {}
    for q, c in cases.items():
        source = c["gold_documents"][0]
        row = {"id": q, "source": source, "pool": evidence(c, pools[q]["pool"]), "arms": {}}
        for arm in ARMS:
            task = f"{q}/{arm}"
            e = events[task]
            vote = final.get(task)
            labels = {a: lab(c, reviews.get(q), a) for a in ("official", "source")}
            item = {
                "status": e["status"],
                "first_success": e["status"] == "ok",
                "labels": labels,
                "insufficient": e.get("insufficient"),
                "visible": evidence(c, e["visible"]) if "visible" in e else None,
                "vote": vote,
                "latency_ms": e.get("total_ms"),
                "model_and_assembly_ms": e.get("model_and_assembly_ms"),
            }
            for axis, label in labels.items():
                item[axis] = quality(e, label, vote)
            visible = {v["number"]: v for v in e.get("visible", [])}
            pool = {v["chunk_id"]: v for v in pools[q]["pool"]}
            item["cited_count"] = len(e.get("citations", []))
            item["locatable_count"] = sum(
                n in visible
                and visible[n]["chunk_id"] in pool
                and all(
                    visible[n][k] == pool[visible[n]["chunk_id"]][k]
                    for k in ("document_id", "document_revision_id", "locator", "text")
                )
                for n in e.get("citations", [])
            )
            row["arms"][arm] = item
        rows.append(row)
    for arm in ARMS:
        items = [r["arms"][arm] for r in rows]
        metrics = {}
        for axis in ("official", "source"):
            eligible = [v for v in items if v[axis] is not None]
            metrics[axis] = {
                "scored": len(eligible),
                "success": sum(v[axis] for v in eligible),
                "rate": sum(v[axis] for v in eligible) / len(eligible) if eligible else None,
                "answerable": sum(v["labels"][axis] == "answerable" for v in eligible),
                "unanswerable": sum(v["labels"][axis] == "unanswerable" for v in eligible),
                "correct_refusals": sum(
                    v["insufficient"] is True and v[axis] == 1 for v in eligible
                ),
                "wrong_refusals": sum(
                    v["insufficient"] is True and v["labels"][axis] == "answerable"
                    for v in eligible
                ),
            }
        retrieval = {}
        for stage in ("pool", "visible"):
            measures = [
                r["pool"] if stage == "pool" else r["arms"][arm]["visible"]
                for r in rows
                if not cases[r["id"]]["negative"]
            ]
            retrieval[stage] = {
                k: {
                    "hits": sum(bool(m[k]) for m in measures if m is not None),
                    "measured": sum(m is not None and m[k] is not None for m in measures),
                    "official_answerable": len(measures),
                }
                for k in ("gold_document", "any_gold_passage_in_one_chunk", "any_answer_string")
            }
        by_arm[arm] = {
            **metrics,
            "literal_retrieval": retrieval,
            "status_counts": dict(Counter(v["status"] for v in items)),
            "first_success": sum(v["first_success"] for v in items),
            "dispatched": sum(v["status"] != "not_run" for v in items),
            "unsupported_assertions": sum(
                v["vote"]["unsupported_assertion"] for v in items if v["vote"]
            ),
            "condition_omissions": sum(v["vote"]["condition_omission"] for v in items if v["vote"]),
            "sufficient_visible": sum(v["vote"]["visible_sufficient"] for v in items if v["vote"]),
            "supported_citations": sum(
                sum(v["vote"]["citation_support"].values()) for v in items if v["vote"]
            ),
            "cited_count": sum(v["cited_count"] for v in items),
            "locatable_count": sum(v["locatable_count"] for v in items),
            "total_ms_p50": percentile(
                [v["latency_ms"] for v in items if v["latency_ms"] is not None], 0.5
            ),
            "total_ms_p95": percentile(
                [v["latency_ms"] for v in items if v["latency_ms"] is not None], 0.95
            ),
        }
    result = {
        "rows": rows,
        "arms": by_arm,
        "disagreements": len(conflicts),
        "ledger": billing(args.ledger, events),
        "review_files_sha256": [
            digest(read(p)) for p in (args.first, args.second, args.adjudicated) if p
        ],
        "paired": {
            f"{b}-{a}/{axis}": paired(rows, b, a, axis)
            for a, b in combinations(ARMS, 2)
            for axis in ("official", "source")
        },
    }
    # The source audit is the pre-output adjudication. Keep the official analysis visible.
    baseline = result["paired"]["D-B/source"]
    strong = result["paired"]["D-C/source"]
    complete = all(
        v["status"] not in {"not_run", "unknown_outcome", "stopped"}
        for r in rows
        for v in r["arms"].values()
    ) and not any(group.get("unknown_dispatch_or_response") for group in result["ledger"].values())
    eligible = bool(
        complete
        and baseline["n"]
        and baseline["delta"] >= 0.05
        and baseline["paired_source_ci95"][0] > 0
    )
    result["inference"] = {
        "complete": complete,
        "primary_axis": "source",
        "D_vs_B": "REQUIRES_FAILURE_REVIEW" if eligible else "INCONCLUSIVE",
        "D_vs_C_positive_interval": bool(
            complete and strong["n"] and strong["paired_source_ci95"][0] > 0
        ),
        "D_new_failures_vs_B": [
            r["id"] for r in rows if (r["arms"]["B"]["source"], r["arms"]["D"]["source"]) == (1, 0)
        ],
    }
    write(args.out / "report.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "report"])
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--pools", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, default=OUT)
    for name in ("ledger", "first", "second", "adjudicated"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.action == "export":
        export(args)
    elif not all((args.ledger, args.first, args.second)):
        parser.error("report requires --ledger, --first and --second")
    elif report(args) is None:
        print("Blind adjudication required: disagreements.json")
    else:
        print("Wrote report.json")


if __name__ == "__main__":
    main()
