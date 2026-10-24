"""Describe exposed scenario results without inference or method reselection."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import fmean
from types import SimpleNamespace

from benchmarks.public_rag.scenario_metrics import evidence_metrics
from benchmarks.public_rag.scenario_runner import dump, load


def diagnose(run: Path, data_root: Path) -> None:
    target = run / "diagnostics.json"
    if target.exists():
        raise ValueError("refusing to overwrite historical diagnostics")
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    meta = report["scope"]
    data = load(SimpleNamespace(dataset=meta["dataset"], split=meta["split"], data=data_root))
    queries = {q["query_id"]: q for q in data["queries"]}
    rows = [
        json.loads(line) for line in (run / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [
        r
        for r in rows
        if r["answerable"]
        and r["evidence_alignment_complete"]
        and r["unaligned_evidence_count"] == 0
        and all(a["evaluable"] for a in r["arms"].values())
    ]
    if len(rows) != report["denominators"]["evidence_evaluable"]:
        raise ValueError("diagnostic denominator differs from frozen report")
    profiles = meta["profiles"]
    pools, attribution = {}, {}
    for profile in profiles:
        metrics, failures = [], Counter()
        for row in rows:
            arm = row["arms"][profile]
            pool = evidence_metrics(
                queries[row["query_id"]]["evidence_sets"],
                [item["document_id"] for item in arm["rerank_ranking"]],
            )
            metrics.append(pool)
            if arm["complete_evidence_set"]:
                failures["complete_after_budget"] += 1
            elif pool["complete_evidence_set"]:
                failures["complete_in_pool_but_lost_during_context_selection"] += 1
            else:
                failures["no_complete_evidence_set_in_candidate_pool"] += 1
        pools[profile] = {
            metric: fmean(row[metric] for row in metrics)
            for metric in ("best_evidence_recall", "complete_evidence_set")
        }
        attribution[profile] = dict(failures)
    strata = {}
    if meta["dataset"] == "musique":
        for hops in sorted({row["hops"] for row in rows}):
            group = [row for row in rows if row["hops"] == hops]
            strata[str(hops)] = {
                "questions": len(group),
                "profiles": {
                    profile: {
                        metric: fmean(row["arms"][profile][metric] for row in group)
                        for metric in (
                            "best_evidence_recall",
                            "complete_evidence_set",
                            "evidence_bytes",
                        )
                    }
                    for profile in profiles
                },
            }
    result = {
        "dataset": meta["dataset"],
        "questions": len(rows),
        "scope": "Posthoc descriptive diagnostics on already exposed evidence labels; "
        "no new inference, parameter selection, answer score or independent confirmation.",
        "pool_scope": "Dense top20 passed to BGE reranker within specified paper/case; "
        "paragraph-hit ceiling, not exact support-span coverage or corpus-wide recall.",
        "candidate_pool_metrics": pools,
        "complete_set_attribution": attribution,
        "hop_strata": strata,
    }
    dump(target, result)
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    diagnose(args.run, args.data)


if __name__ == "__main__":
    main()
