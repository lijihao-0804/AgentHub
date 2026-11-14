"""Publish auditable first-answer and wire receipts without redistributing paper passages."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from benchmarks.public_rag.confirmation_cases import OUT, read, save
from benchmarks.public_rag.confirmation_meter import INPUT_PRICE, LIMITS, OUTPUT_PRICE, PER_REQUEST


def sha(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def reconcile(ledger):
    with closing(sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        cap = db.execute("SELECT cap FROM authorization").fetchone()[0]
        rows = [dict(r) for r in db.execute("SELECT * FROM wires ORDER BY id")]
    summary = {}
    for stage, (limit, per_task) in LIMITS.items():
        group = [r for r in rows if r["stage"] == stage]
        if (
            len(group) > limit
            or max(Counter(r["task"] for r in group).values(), default=0) > per_task
        ):
            raise ValueError("Physical request limit exceeded")
        returned = [r for r in group if r["status"] == "returned"]
        unknown = [r for r in group if r["status"] == "unsettled"]
        if len(returned) + len(unknown) != len(group):
            raise ValueError("Unrecognized ledger status")
        for r in returned:
            if (
                not r["provider_id"]
                or r["cost"] != r["input"] * INPUT_PRICE + r["output"] * OUTPUT_PRICE
            ):
                raise ValueError("Provider receipt or peak-price reconciliation failed")
        summary[stage] = {
            "reservations": len(group),
            "confirmed_posts": len(returned),
            "unsettled": len(unknown),
            "input_tokens": sum(r["input"] for r in returned),
            "output_tokens": sum(r["output"] for r in returned),
            "estimated_microcny": sum(r["cost"] for r in returned),
            "unsettled_reserve_microcny": sum(r["cost"] for r in unknown),
        }
    if (
        sum(v["reservations"] for v in summary.values()) != len(rows)
        or sum(r["cost"] for r in rows) > cap
    ):
        raise ValueError("Ledger limits differ from authorization")
    unresolved = [r for r in rows if r["status"] != "returned"]
    if unresolved and (
        len(unresolved) != 1 or unresolved[0] != rows[-1] or rows[-1]["cost"] != PER_REQUEST
    ):
        raise ValueError("Stop rule violated")
    return {"cap_microcny": cap, "stages": summary, "wires": rows, "provider_invoice_cny": None}


def technical(events, report, blind_dir):
    raw = [json.loads(line) for line in events.read_text(encoding="utf8").splitlines()]
    answers = {r["task"]: r for r in raw if "task" in r}
    expected = {k["id"]: k for k in read(OUT / "cohort.json")["cases"]}
    scores = read(report)
    custody = read(blind_dir / "private.json")
    reviews = {
        who: {v["task"]: t for t, v in group.items()} for who, group in custody["reviewers"].items()
    }
    cards = []
    for row in scores["rows"]:
        q = row["id"]
        for arm, judgment in row["arms"].items():
            task = q + "/" + arm
            response = answers.get(task, {})
            events_seen = []
            for event in response.get("events", []):
                events_seen.append(
                    {
                        "stage": event["stage"],
                        "messages_sha256": sha(event["messages"]),
                        "provider_id": event.get("id"),
                        "model": event.get("model"),
                        "usage": event.get("usage"),
                        "finish_reason": event.get("finish_reason"),
                        "raw_visible_answer": (event.get("raw_message") or {}).get("content"),
                    }
                )
            cards.append(
                {
                    "id": q,
                    "arm": arm,
                    "question": expected[q]["query"],
                    "status": judgment["status"],
                    "labels": judgment["labels"],
                    "official_score": judgment["official"],
                    "source_score": judgment["source"],
                    "answer": response.get("answer"),
                    "insufficient": response.get("insufficient"),
                    "citations": response.get("citations"),
                    "visible": [
                        {
                            k: v[k]
                            for k in (
                                "number",
                                "document_id",
                                "document_revision_id",
                                "chunk_id",
                                "locator",
                            )
                        }
                        | {"text_sha256": sha(v["text"])}
                        for v in response.get("visible", [])
                    ],
                    "blind_tokens": {w: m.get(task) for w, m in reviews.items()},
                    "model_events": events_seen,
                }
            )
    return cards


def refusal_kind(item):
    if item["status"] == "citations_on_refusal":
        return "citations_on_refusal"
    if item["status"] != "ok":
        return "other_non_ok"
    if item["insufficient"]:
        return "valid_structured_refusal"
    if item["vote"]["refusal_justified"]:
        return "justified_text_refusal_flag_false"
    return "other_valid_answer"


def failed_criteria(item, axis):
    """Explain a zero using the predicates in the frozen quality function."""
    if item[axis] != 0:
        return []
    if item["status"] != "ok":
        return ["status:" + item["status"]]
    vote = item["vote"]
    issues = []
    if vote["unsupported_assertion"]:
        issues.append("unsupported_assertion")
    if item["insufficient"]:
        if item["labels"][axis] != "unanswerable":
            issues.append("structured_refusal_on_answerable")
        if not vote["refusal_justified"]:
            issues.append("refusal_not_justified")
        if vote["visible_sufficient"]:
            issues.append("sufficient_evidence_for_refused_question")
    else:
        if item["labels"][axis] != "answerable":
            issues.append("answer_flag_on_unanswerable")
        for field in (
            "visible_sufficient",
            "correct_content",
            "conditions_complete",
            "critical_claims_supported",
        ):
            if not vote[field]:
                issues.append(field + ":false")
        if vote["condition_omission"]:
            issues.append("condition_omission")
        if not vote["citation_support"]:
            issues.append("no_citations")
        issues.extend(
            "unsupported_citation:" + n
            for n, supported in vote["citation_support"].items()
            if not supported
        )
    if not issues:
        raise ValueError("Zero score without a failed frozen predicate")
    return issues


def comparisons(report, cards):
    """Join public receipts to frozen scores; all rates retain their denominators."""
    rows = report["rows"]
    receipts = {(c["id"], c["arm"]): c for c in cards}
    if len(receipts) != len(cards) or set(receipts) != {
        (r["id"], arm) for r in rows for arm in ("B", "C", "D")
    }:
        raise ValueError("Missing, extra or duplicate first-answer receipt")
    pairs = {}
    refusal_audit = {}
    for axis in ("source", "official"):
        for arm in ("B", "C", "D"):
            groups = {
                k: {"denominator": 0, "scored": 0, "kinds": Counter(), "correct_structured": 0}
                for k in ("all", "answerable", "unanswerable", "ambiguous")
            }
            for row in rows:
                item = row["arms"][arm]
                card = receipts[row["id"], arm]
                if (
                    card["status"] != item["status"]
                    or card["labels"] != item["labels"]
                    or card[axis + "_score"] != item[axis]
                    or card["insufficient"] != item["insufficient"]
                ):
                    raise ValueError("First-answer receipt differs from frozen score")
                kind = refusal_kind(item)
                for key in ("all", item["labels"][axis]):
                    group = groups[key]
                    group["denominator"] += 1
                    group["scored"] += item[axis] is not None
                    group["kinds"][kind] += 1
                    group["correct_structured"] += (
                        kind == "valid_structured_refusal" and item[axis] == 1
                    )
            refusal_audit[f"{arm}/{axis}"] = groups
        for left, right in (("B", "D"), ("B", "C"), ("C", "D")):
            entries = []
            counts = dict.fromkeys(("gain", "loss", "tie", "excluded"), 0)
            for row in rows:
                before, after = row["arms"][left], row["arms"][right]
                a, b = before[axis], after[axis]
                if a is None or b is None:
                    change = "excluded"
                elif a == b:
                    change = "tie"
                else:
                    change = "gain" if b > a else "loss"
                counts[change] += 1
                details = {}
                for arm, item in ((left, before), (right, after)):
                    details[arm] = {
                        "score": item[axis],
                        "status": item["status"],
                        "refusal_kind": refusal_kind(item),
                        "failed_criteria": failed_criteria(item, axis),
                    }
                entries.append(
                    {
                        "id": row["id"],
                        "question": receipts[row["id"], right]["question"],
                        "source": row["source"],
                        "label": after["labels"][axis],
                        "change": change,
                        "arms": details,
                    }
                )
            expected = report["paired"][f"{right}-{left}/{axis}"]
            if [counts[k] for k in ("gain", "loss", "tie")] != [
                expected[k] for k in ("win", "loss", "tie")
            ] or len(rows) - counts["excluded"] != expected["n"]:
                raise ValueError("Pair counts differ from frozen report")
            pairs[f"{left}->{right}/{axis}"] = {
                "counts": counts,
                "paired_denominator": expected["n"],
                "items": entries,
            }
    return {"pairs": pairs, "refusals": refusal_audit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--public-run", type=Path, required=True)
    parser.add_argument("--blind-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    save(args.output / "billing.json", reconcile(args.ledger))
    report_path = args.blind_dir / "report.json"
    cards = technical(args.public_run / "events.jsonl", report_path, args.blind_dir)
    save(args.output / "public-first-answers.json", cards)
    save(args.output / "public-comparisons.json", comparisons(read(report_path), cards))
