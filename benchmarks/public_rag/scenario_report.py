"""Complete-split scoring and frozen selection for scenario retrieval experiments."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.scenario_runner import dump


def score(run: Path, protocol: dict, frozen: Path | None):
    from benchmarks.public_rag.scenario_metrics import paired_group_ci

    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    if meta["protocol"] != protocol:
        raise ValueError("Scoring protocol differs from measured method")
    done = json.loads((run / "completed.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (run / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(rows) != done["expected"] or len(rows) != meta["queries"]:
        raise ValueError("full planned capture required; no partial-split scoring")
    if len({r["query_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate query results")
    eligible = [
        r
        for r in rows
        if r["answerable"]
        and r["evidence_alignment_complete"]
        and r["unaligned_evidence_count"] == 0
        and all(a["evaluable"] for a in r["arms"].values())
    ]
    if not eligible:
        raise ValueError("no fully aligned answerable questions; diagnose data before selection")
    dataset = meta["dataset"]
    gates_config = protocol[dataset]["gate"]
    primary = "best_evidence_recall" if dataset == "qasper" else "complete_evidence_set"
    metrics = [
        "best_evidence_recall",
        "complete_evidence_set",
        "evidence_precision",
        "full_text_evidence_recall",
        "selected_paragraphs",
        "evidence_bytes",
    ]
    profiles = meta["profiles"]
    averages = {p: {m: fmean(r["arms"][p][m] for r in eligible) for m in metrics} for p in profiles}
    alternatives = [p for p in profiles if p != "plain_small"]
    confidence = 0.95 if frozen else 1 - 0.05 / max(1, len(alternatives))
    if not frozen and "chain_selection" in alternatives:
        confidence = 1 - 0.05 / (len(alternatives) + 1)
    comparisons, passed = {}, []
    for candidate in alternatives:
        ci = paired_group_ci(
            eligible, primary, "plain_small", candidate, confidence=confidence, samples=5000
        )
        delta = averages[candidate][primary] - averages["plain_small"][primary]
        guard = "complete_evidence_set" if dataset == "qasper" else "best_evidence_recall"
        loss = averages["plain_small"][guard] - averages[candidate][guard]
        min_gain = gates_config[
            "min_evidence_recall_gain" if dataset == "qasper" else "min_complete_set_gain"
        ]
        max_loss = gates_config[
            "max_complete_set_loss" if dataset == "qasper" else "max_recall_loss"
        ]
        gates = {
            "minimum_primary_gain": delta >= min_gain,
            "cluster_ci_above_zero": ci["inference_valid"] and ci["ci_low"] > 0,
            "maximum_secondary_loss": loss <= max_loss,
            "adequate_questions_and_groups": len(eligible) >= 100
            and len({r["group_id"] for r in eligible}) >= 20,
        }
        if candidate == "chain_selection":
            statuses = Counter(r["api"]["status"] for r in rows)
            gates["valid_api_fraction"] = statuses["VALID"] / len(rows) >= 0.95
            gates["unknown_outcomes_zero"] = statuses["UNKNOWN_OUTCOME"] == 0
            incremental = paired_group_ci(
                eligible, primary, "heading_fill", candidate,
                confidence=confidence, samples=5000,
            )
            gates["incremental_gain_over_budget_fill"] = incremental["delta"] >= min_gain
            gates["incremental_ci_above_zero"] = incremental["ci_low"] > 0
        comparisons[candidate] = {"primary_ci": ci, "gates": gates, "passed": all(gates.values())}
        if candidate == "chain_selection":
            comparisons[candidate]["versus_heading_fill"] = incremental
        if all(gates.values()):
            passed.append(candidate)
    selected = max(passed, key=lambda p: averages[p][primary]) if passed else "plain_small"
    result = {
        "scope": meta,
        "primary": primary,
        "metrics": averages,
        "comparisons": comparisons,
        "denominators": {
            "all_questions": len(rows),
            "evidence_evaluable": len(eligible),
            "unanswerable": sum(not r["answerable"] for r in rows),
            "unaligned": sum(
                not r["evidence_alignment_complete"] or r["unaligned_evidence_count"] > 0
                for r in rows
            ),
        },
        "budget_outcomes": {
            p: dict(Counter(r["arms"][p]["status"] for r in rows)) for p in profiles
        },
        "cost_note": "same maximum context budget, not equal actual input bytes; "
        "shared cached profile inference, no paired E2E latency claim",
        "generated_answer_evaluated": False,
        "production_integrated": False,
    }
    if frozen:
        bound = json.loads(frozen.read_text(encoding="utf-8"))["selected_profile"]
        result["confirmation"] = {
            "frozen_profile": bound,
            "passed": comparisons.get(bound, {}).get("passed", False),
            "test_reselection": False,
        }
    else:
        result["selected_profile"] = selected
    if rows and "api" in rows[0]:
        result["api_outcomes"] = dict(Counter(r["api"]["status"] for r in rows))
        attempted = [r["api"] for r in rows if r["api"]["status"] != "BUDGET_FALLBACK"]
        usage = [r.get("usage") for r in attempted]
        result["provider_usage"] = {
            key: {
                "sum": sum(u[key] for u in usage if u is not None and key in u)
                if any(u is not None and key in u for u in usage)
                else None,
                "observed_count": sum(u is not None and key in u for u in usage),
                "unknown_count": sum(u is None or key not in u for u in usage),
            }
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        result["incremental_request_mean_ms"] = (
            fmean(r["request_ms"] for r in attempted) if attempted else None
        )
        result["reasoning_tokens_note"] = (
            "Preserved in raw provider usage when reported; absent is unknown."
        )
        result["cost_note"] += "; no pricing snapshot, no currency cost claim"
    dump(run / "report.json", result)
    if not frozen:
        dump(
            run / "frozen-selection.json",
            {
                "dataset": dataset,
                "selected_profile": selected,
                "development_run": run.as_posix(),
                "protocol": protocol,
            },
        )
    print(
        json.dumps(
            {
                "dataset": dataset,
                "eligible": len(eligible),
                "metrics": averages,
                "selected_profile": selected if not frozen else "NOT_RESELECTED",
            },
            indent=2,
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--frozen", type=Path)
    args = p.parse_args()
    if (args.run / "report.json").exists():
        raise ValueError("refusing to overwrite historical scores")
    score(args.run, json.loads(args.protocol.read_text(encoding="utf-8")), args.frozen)


if __name__ == "__main__":
    main()
