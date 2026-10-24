"""Compare paired, interleaved retrieval runs and audit exact ranking changes."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from statistics import fmean

from benchmarks.public_rag.data import load_scifact, query_groups
from benchmarks.public_rag.scifact_report import quantile, read_run
from benchmarks.public_rag.scifact_runner import DATA, dump


def grouped_difference(
    baseline: dict[str, float],
    candidate: dict[str, float],
    groups: dict[str, str],
    *,
    samples: int = 10000,
    seed: int = 20261021,
) -> dict:
    """Cluster percentile bootstrap for the query-weighted paired mean difference."""
    if set(baseline) != set(candidate) or not baseline:
        raise ValueError("paired timing samples must contain the same nonempty query set")
    if samples < 1:
        raise ValueError("samples must be positive")
    clusters = {}
    for qid in sorted(baseline):
        clusters.setdefault(groups[qid], []).append(candidate[qid] - baseline[qid])
    # Equivalent cluster resampling, without constructing large arrays each draw.
    stats = [(sum(values), len(values)) for values in clusters.values()]
    rng = Random(seed)
    differences = []
    for _ in range(samples):
        numerator, denominator = 0.0, 0
        for _ in stats:
            total, count = stats[rng.randrange(len(stats))]
            numerator += total
            denominator += count
        differences.append(numerator / denominator)
    return {
        "estimate_ms": fmean(candidate[q] - baseline[q] for q in baseline),
        "ci95_low_ms": quantile(differences, 0.025),
        "ci95_high_ms": quantile(differences, 0.975),
        "queries": len(baseline),
        "groups": len(stats),
        "bootstrap_samples": samples,
        "sign": "optimized minus baseline; negative is faster",
    }


def compare(baseline_path: Path, optimized_path: Path, data: Path) -> dict:
    baseline_meta, baseline = read_run(baseline_path)
    optimized_meta, optimized = read_run(optimized_path)
    for key in ("split", "queries", "seed", "snapshot_id", "retrieval_identity"):
        if baseline_meta[key] != optimized_meta[key]:
            raise ValueError(f"unpaired run identity: {key}")
    if not baseline_meta["paired_interleaved"] or not optimized_meta["paired_interleaved"]:
        raise ValueError("primary performance comparison requires interleaved runs")
    if set(baseline) != set(optimized):
        raise ValueError("different paired query sets")
    if baseline_meta["split"] == "test":
        with baseline_path.with_name("test-exposures.jsonl").open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "event": "TEST_TIMING_ANALYSIS_LABEL_ACCESS",
                        "at": datetime.now(UTC).isoformat(),
                        "baseline": baseline_path.name,
                        "optimized": optimized_path.name,
                    }
                )
                + "\n"
            )
    source = load_scifact(data, baseline_meta["split"])
    if set(baseline) != set(source.qrels):
        raise ValueError("paired report must cover the complete official split")
    groups = query_groups(source.qrels)
    warm = [qid for qid in baseline if not baseline[qid]["first_inference"]]
    mismatches = {stage: [] for stage in ("dense", "sparse", "fusion", "rerank")}
    max_score_differences = {stage: 0.0 for stage in mismatches}
    first_counts = {"baseline": 0, "optimized": 0}
    for qid in baseline:
        b, o = baseline[qid], optimized[qid]
        if b["first_inference"] != o["first_inference"] or b["pair_order"] != o["pair_order"]:
            raise ValueError("unpaired warmup/order metadata")
        if not b["first_inference"]:
            first_counts[b["pair_order"][0]] += 1
        for stage in mismatches:
            left, right = b["stages"][stage]["ranking"], o["stages"][stage]["ranking"]
            if [hit["chunk_id"] for hit in left] != [hit["chunk_id"] for hit in right]:
                mismatches[stage].append(qid)
            elif left:
                max_score_differences[stage] = max(
                    max_score_differences[stage],
                    max(abs(x["score"] - y["score"]) for x, y in zip(left, right, strict=True)),
                )
    timings = {}
    for stage in ("total", "snapshot_scope", "chunk_load", "dense", "sparse", "fusion", "rerank"):

        def values(rows, field=stage):
            if field == "total":
                return {qid: rows[qid]["total_ms"] for qid in warm}
            if field in ("snapshot_scope", "chunk_load"):
                return {qid: rows[qid]["db_ms"][field] for qid in warm}
            return {qid: rows[qid]["stages"][field]["latency_ms"] for qid in warm}

        b_values, o_values = values(baseline), values(optimized)
        timings[stage] = {
            "baseline_mean_ms": fmean(b_values.values()),
            "optimized_mean_ms": fmean(o_values.values()),
            "baseline_p50_ms": quantile(list(b_values.values()), 0.5),
            "optimized_p50_ms": quantile(list(o_values.values()), 0.5),
            "baseline_p95_ms": quantile(list(b_values.values()), 0.95),
            "optimized_p95_ms": quantile(list(o_values.values()), 0.95),
            **grouped_difference(b_values, o_values, groups),
        }
    # Randomized order is recorded so a speed claim can be checked in both
    # first/second strata and throughout the session, without additional runs.
    order_strata = {}
    time_quartiles = {}
    for first in ("baseline", "optimized"):
        qids = [qid for qid in warm if baseline[qid]["pair_order"][0] == first]
        order_strata[first + "_first"] = {
            "n": len(qids),
            "mean_difference_ms": fmean(
                optimized[qid]["total_ms"] - baseline[qid]["total_ms"] for qid in qids
            )
            if qids
            else None,
        }
    for quartile in range(4):
        qids = [
            qid for qid in warm if min(3, baseline[qid]["ordinal"] * 4 // len(baseline)) == quartile
        ]
        time_quartiles[str(quartile + 1)] = {
            "n": len(qids),
            "mean_difference_ms": fmean(
                optimized[qid]["total_ms"] - baseline[qid]["total_ms"] for qid in qids
            )
            if qids
            else None,
        }
    return {
        "split": baseline_meta["split"],
        "query_count": len(baseline),
        "warm_query_count": len(warm),
        "first_in_pair_counts": first_counts,
        "ranking_mismatches": mismatches,
        "max_score_differences": max_score_differences,
        "timings": timings,
        "order_strata": order_strata,
        "time_quartiles": time_quartiles,
        "limitations": (
            "One serial randomized session; CI reflects query/source mix, not variation across "
            "sessions or hardware. DB hydration optimization retains full Qdrant revision filter."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.baseline, args.optimized, args.data)
    dump(args.output, result)
    print(
        json.dumps(
            {
                "split": result["split"],
                "queries": result["query_count"],
                "ranking_mismatches": {k: len(v) for k, v in result["ranking_mismatches"].items()},
                "total": result["timings"]["total"],
                "chunk_load": result["timings"]["chunk_load"],
            }
        )
    )


if __name__ == "__main__":
    main()
