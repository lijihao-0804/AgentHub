"""Stage-by-stage record-preservation diagnostics, without model inference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.scenario_runner import dump


def report(folder):
    target = folder / "stage-report.json"
    if target.exists():
        raise ValueError("historical stage report exists")
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    done = json.loads((folder / "completed.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (folder / "prepared.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(rows) != meta["queries"] or len(rows) != done["expected"]:
        raise ValueError("full prepared split required")
    if len({r["query_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate questions")
    eligible = [
        r
        for r in rows
        if r["answerable"]
        and r["evidence_alignment_complete"]
        and r["unaligned_evidence_count"] == 0
    ]
    if not eligible:
        raise ValueError("no evidence-evaluable questions")
    metrics = (
        "best_evidence_recall",
        "complete_evidence_set",
        "full_text_evidence_recall",
        "records",
    )
    profiles = {}
    for profile in meta["profiles"]:
        arms = [r["arms"][profile] for r in eligible]
        profiles[profile] = {
            "stages": {
                stage: {
                    metric: fmean(a["stages"][stage][metric] for a in arms) for metric in metrics
                }
                for stage in arms[0]["stages"]
            },
            "observation_bytes": fmean(a["observation_bytes"] for a in arms),
            "final_evidence_bytes": fmean(a["final_evidence_bytes"] for a in arms),
            "dropped_exchanges": sum(a["budget"]["dropped_exchange_count"] for a in arms),
            "admitted_questions": sum(bool(a["admitted_chunk_ids"]) for a in arms),
        }
    result = {
        "questions": len(rows),
        "denominators": {
            "all_questions": len(rows),
            "evidence_evaluable": len(eligible),
            "unanswerable": sum(not r["answerable"] for r in rows),
            "unaligned": sum(
                not r["evidence_alignment_complete"] or r["unaligned_evidence_count"] > 0
                for r in rows
            ),
        },
        "profile_average_population": "evidence-evaluable subset; all capture retained separately",
        "profiles": profiles,
        "scope": "Actual consumer methods, controlled snapshot/vector/score I/O; "
        "complete recoverable text records only. Legacy preview trailing partial text "
        "may be visible to the answer model but is not credited as a complete record. "
        "Not live database/authorization/checkpoint validation or generated answer score.",
        "same_budget_not_equal_actual_input": True,
        "full_text_metric": "Secondary90% source-character coverage proxy, not support spans",
        "no_rag_gold": "Diagnostic controls excluded from method adoption",
    }
    dump(target, result)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    args = parser.parse_args()
    report(args.prepared)


if __name__ == "__main__":
    main()
