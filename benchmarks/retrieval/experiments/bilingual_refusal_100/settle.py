"""Reconcile the three disjoint bilingual experiment ledgers into public CNY totals."""

import argparse
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from benchmarks.retrieval.experiments.first_refusal_100.attempts import reconcile
from benchmarks.retrieval.experiments.first_refusal_100.runner import file_hash

from .history import previous, reconcile_history
from .review import private, read_json, rows


def summary(root):
    private(root)
    report, seen, grand = {}, set(), Counter()
    for folder, group, size in (
        ("old100-live", "old100", 100),
        ("wai14-live", "wai14", 14),
        ("independent", "independent", 200),
    ):
        path = root / folder
        freeze = read_json(path / "freeze.json")
        if freeze.get("offline", freeze.get("offline_replay", True)):
            raise ValueError("Expected paid observations")
        if group == "independent":
            audit = reconcile(path)
        else:
            cases, _, _ = previous(group)
            audit = reconcile_history(path, cases, freeze)
        execution = read_json(path / "execution.json")
        if any(execution[k] != v for k, v in audit.items()):
            raise ValueError("Persisted execution and receipts differ")
        arms = defaultdict(Counter)
        for r in rows(path / "receipts.jsonl"):
            if r["state"] != "confirmed":
                continue
            if r["id"] in seen:
                raise ValueError("Provider receipt used twice")
            seen.add(r["id"])
            arm = arms[r["key"].split("/")[-1]]
            arm.update(
                calls=1,
                http=int(r["attempt"] == 1),
                corrections=int(r["attempt"] > 1),
                input_tokens=r["input"],
                cached_input_tokens=r["cached"],
                output_tokens=r["output"],
                estimated_cny=Decimal(r["estimated_cny"]),
            )
        total = Counter()
        for counts in arms.values():
            total.update(counts)
        if total["http"] != size or total["estimated_cny"] != Decimal(execution["estimated_cny"]):
            raise ValueError("Unexpected HTTP count or cost")
        grand.update(total)
        report[group] = {
            "arms": dict(arms),
            "total": total,
            "source_commit": freeze["source_commit"],
            "registered_cases_sha256": freeze["cases_sha256"],
            "receipts_file_sha256": file_hash(path / "receipts.jsonl"),
        }
    if grand["calls"] > 628 or grand["estimated_cny"] > Decimal("11.575296"):
        raise ValueError("Experiment budget exceeded")
    return {"runs": report, "total": grand, "unique_confirmed_receipts": len(seen), "unknown": 0}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("root", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    result = summary(args.root)
    with args.output.open("x", encoding="utf8") as out:
        json.dump(result, out, indent=2, default=str)
        out.write("\n")
