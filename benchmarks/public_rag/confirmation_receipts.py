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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--public-run", type=Path, required=True)
    parser.add_argument("--blind-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    save(args.output / "billing.json", reconcile(args.ledger))
    save(
        args.output / "public-first-answers.json",
        technical(args.public_run / "events.jsonl", args.blind_dir / "report.json", args.blind_dir),
    )
