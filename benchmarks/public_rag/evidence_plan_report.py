"""Score the single preregistered compact evidence projection; never call a model."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.consumer_report import (
    _api_summary,
    _citation_summary,
    _hop_strata,
    _metric_means,
    _validate_row,
)
from benchmarks.public_rag.scenario_metrics import paired_group_ci
from benchmarks.public_rag.scenario_runner import dump


def score(rows, meta, completed, protocol):
    profiles = ["legacy5", "compact5", "compact5_matched"]
    if meta["protocol"] != protocol or meta["profiles"] != profiles:
        raise ValueError("scoring protocol or fixed profiles differ")
    if (
        not (len(rows) == meta["queries"] == completed["queries"] == completed["expected"])
        or not rows
    ):
        raise ValueError("full nonempty capture required")
    if len({r["query_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate questions")
    for number, row in enumerate(rows, 1):
        _validate_row(row, profiles=profiles, line_number=number)
    arms = {
        p: {
            "metrics": _metric_means(rows, p),
            "api": _api_summary(rows, p),
            "citation": _citation_summary(rows, p),
        }
        for p in profiles
    }
    cfg = protocol["comparison"]
    ci = paired_group_ci(
        rows,
        "token_f1",
        "legacy5",
        "compact5",
        group_key="group_id",
        seed=protocol["seed"],
        samples=cfg["bootstrap_samples"],
        confidence=cfg["confidence"],
    )
    base, candidate = arms["legacy5"], arms["compact5"]
    recall_loss = base["metrics"]["evidence_recall"] - candidate["metrics"]["evidence_recall"]
    common = {
        "run_complete": completed["run_state"] == "COMPLETE",
        "all_pairs_observed": completed["observed_calls"] == len(rows) * len(profiles),
        "both_valid": min(base["api"]["valid_fraction"], candidate["api"]["valid_fraction"])
        >= cfg["min_valid_fraction_each_arm"],
        "unknown_zero": sum(arms[p]["api"]["unknown_outcome_count"] for p in profiles)
        == cfg["unknown_outcomes"],
        "correct_model": all(
            a["api"]["status_counts"].get("MODEL_IDENTITY_ERROR", 0) == 0 for a in arms.values()
        ),
        "support_loss_within_limit": recall_loss <= cfg["max_evidence_recall_loss"],
        "group_inference_valid": ci["inference_valid"],
    }
    efficiency = cfg["efficiency_alternative"]
    usage = {}
    for p in profiles:
        u = arms[p]["api"]["usage"]["prompt_tokens"]
        usage[p] = {
            **u,
            "mean_observed": u["sum"] / u["observed_count"] if u["observed_count"] else None,
        }
    reduction = (
        1 - usage["compact5"]["mean_observed"] / usage["legacy5"]["mean_observed"]
        if usage["legacy5"]["mean_observed"] and usage["compact5"]["mean_observed"] is not None
        else None
    )
    bp95 = base["api"]["request_latency_ms"]["p95"]
    cp95 = candidate["api"]["request_latency_ms"]["p95"]
    ratio = cp95 / bp95 if bp95 and cp95 is not None else None
    quality_gates = {
        "minimum_f1_gain": ci["delta"] >= cfg["min_quality_f1_gain"],
        "positive_ci": ci["ci_low"] > 0,
    }
    efficiency_gates = {
        "f1_noninferiority": ci["ci_low"] > efficiency["f1_delta_ci_lower_above"],
        "input_reduction": reduction is not None
        and reduction >= efficiency["minimum_observed_mean_input_token_reduction"],
        "request_p95": ratio is not None and ratio <= efficiency["maximum_request_p95_ratio"],
        "usage_coverage": all(
            usage[p]["observed_count"] / len(rows)
            >= efficiency["input_usage_required_fraction_each_arm"]
            for p in ("legacy5", "compact5")
        ),
    }
    quality_passed = all(common.values()) and all(quality_gates.values())
    efficiency_passed = all(common.values()) and all(efficiency_gates.values())
    return {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "scope": __doc__,
        "queries": len(rows),
        "groups": len({r["group_id"] for r in rows}),
        "arms": arms,
        "primary_f1_comparison": ci,
        "common_gates": common,
        "quality_gates": quality_gates,
        "efficiency_gates": efficiency_gates,
        "quality_passed": quality_passed,
        "efficiency_passed": efficiency_passed,
        "passed": quality_passed or efficiency_passed,
        "support_recall_loss": recall_loss,
        "observed_input_usage": usage,
        "mean_input_token_reduction": reduction,
        "request_p95_ratio": ratio,
        "strata": _hop_strata(rows, profiles),
        "matched_f1_descriptive_delta": fmean(
            r["arms"]["compact5_matched"]["token_f1"] - r["arms"]["legacy5"]["token_f1"]
            for r in rows
        ),
        "limitations": (
            "Controlled retrieval I/O, provided MuSiQue passages, forced single search, "
            "English short answers; no live SQL/Qdrant/checkpoint E2E, semantic citation "
            "judging, actual-token-matched algorithm claim or pricing snapshot."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--frozen", type=Path)
    args = p.parse_args()
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    meta = json.loads((args.run / "meta.json").read_text(encoding="utf-8"))
    completed = json.loads((args.run / "completed.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (args.run / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    report_path = args.run / "report.json"
    if report_path.exists():
        raise ValueError("refusing existing score report")
    prepared = Path(meta["prepared"])
    prep_meta = json.loads((prepared / "meta.json").read_text(encoding="utf-8"))
    prep_done = json.loads((prepared / "completed.json").read_text(encoding="utf-8"))
    captures = [
        json.loads(s)
        for s in (prepared / "prepared.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if (
        prep_meta["protocol"] != protocol
        or prep_meta["queries"] != prep_done["expected"]
        or len(captures) != len(rows)
    ):
        raise ValueError("prepared capture differs")
    if [c["query_id"] for c in captures] != [r["query_id"] for r in rows]:
        raise ValueError("answer questions differ from preparation")
    if not all(c["arms"]["compact5_matched"]["matches_baseline_fragments"] for c in captures):
        raise ValueError("exact-source diagnostic failed")
    if args.frozen:
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if frozen["protocol"] != protocol or frozen["selected_profile"] != "compact5":
            raise ValueError("no matching frozen candidate")
    report = score(rows, meta, completed, protocol)
    report["mode"] = "confirmation" if args.frozen else "development"
    report["matched_fragments_verified"] = len(captures)
    dump(report_path, report)
    if not args.frozen:
        dump(
            args.run / "frozen-selection.json",
            {
                "at_utc": datetime.now(UTC).isoformat(),
                "protocol": protocol,
                "selected_profile": "compact5" if report["passed"] else "legacy5",
                "development_run": str(args.run),
                "acceptance_basis": "quality"
                if report["quality_passed"]
                else "efficiency"
                if report["efficiency_passed"]
                else "none",
            },
        )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "mode",
                    "queries",
                    "quality_passed",
                    "efficiency_passed",
                    "passed",
                    "mean_input_token_reduction",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
