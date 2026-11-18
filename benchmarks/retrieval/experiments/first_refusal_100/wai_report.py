"""Publish the four WAI observations without provider identifiers or hidden model content."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from benchmarks.retrieval.table_headers import PROTOCOL


def read_rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def details(folder):
    result = json.loads((folder / "results.json").read_bytes())
    ledger = read_rows(folder / "receipts.jsonl")
    receipts = [r for r in ledger if r["state"] == "confirmed"]
    if len({r["id"] for r in receipts}) != len(receipts) or len(ledger) != len(receipts) * 2:
        raise ValueError("Unmatched or duplicate provider receipts.")
    visible = read_rows(folder / "visible-attempts.jsonl")
    by_call = {r["wire_call"]: r for r in visible}
    if len(by_call) != len(visible):
        raise ValueError("A wire call was counted twice.")
    rows = {}
    for i, case in enumerate(result["cases"]):
        generated = case["generated"]
        attempts = []
        for receipt in generated["receipts"]:
            row = by_call.get(receipt["call"])
            if row is None:
                attempts.append({"validation": "no_visible_output_at_validator"})
                continue
            if row["query"] != case["query"]:
                raise ValueError("Observation paired with a different question.")
            content = row["visible_content"]
            try:
                obj = json.loads(content)
            except ValueError:
                obj = None
            attempts.append(
                {
                    "validation": row["validation"],
                    "visible_content": content,
                    "parsed": {
                        k: obj.get(k) for k in ("answer", "citation_ids", "insufficient_evidence")
                    }
                    if isinstance(obj, dict)
                    else None,
                }
            )
        rows[case["query"]] = {
            "attempts": attempts,
            "http": generated["http"],
            "final_joint": generated["joint"],
            "top5_sha256": result["top5_text_sha256"][i],
        }
    if sum(len(r["attempts"]) for r in rows.values()) != len(receipts):
        raise ValueError("Endpoint attempts do not reconcile to the supplier ledger.")
    return rows, {
        "prompt_sha256": result["prompt_sha256"],
        "calls": len(receipts),
        "estimated_cny": str(sum(Decimal(r["estimated_cny"]) for r in receipts)),
    }


def report(baseline, candidate):
    old, old_summary = details(baseline)
    new, new_summary = details(candidate)
    protocol = json.loads(PROTOCOL.read_bytes())
    prior = json.loads(
        (PROTOCOL.parent / "results/table-headers-aliyun-live-qa-20261118.json").read_bytes()
    )
    historical = {r["case"]: r for r in prior["cases"] if r["arm"] == "candidate"}
    diagnostic, controls = [], []
    for i, (query, _, _, gold, _) in enumerate(protocol["cases"], 1):
        history = historical[i]
        previous = {"calls": history["calls"], "http": history["http"], "joint": history["joint"]}
        row = new[query]
        if gold is not None:
            controls.append(
                {
                    "case": i,
                    "prior": previous,
                    "candidate_http": row["http"],
                    "candidate_calls": len(row["attempts"]),
                    "candidate_joint": row["final_joint"],
                }
            )
        else:
            diagnostic.append(
                {
                    "case": i,
                    "question": query,
                    "prior": previous,
                    "baseline_replay": old[query],
                    "candidate_replay": row,
                    "same_top5": row["top5_sha256"] == old[query]["top5_sha256"],
                }
            )
    return {
        "prior_visible_bodies_retained": False,
        "baseline": old_summary,
        "candidate": new_summary,
        "diagnostic": diagnostic,
        "positive_controls": controls,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    data = report(args.baseline, args.candidate)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
