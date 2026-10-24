"""Score the preregistered evidence-set primary; reading order is diagnostic only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.consumer_report import (
    _api_summary,
    _citation_summary,
    _hop_strata,
    _metric_means,
    _validate_row,
)
from benchmarks.public_rag.evidence_set_answers import request_groups
from benchmarks.public_rag.evidence_set_runner import PROFILES
from benchmarks.public_rag.scenario_metrics import _percentile, paired_group_ci
from benchmarks.public_rag.scenario_runner import dump


def usage(value, field):
    observed = value.get("usage")
    n = observed.get(field) if isinstance(observed, dict) else None
    return n if type(n) is int and n >= 0 else None


def score(rows, captures, meta, done, protocol, structure):
    profiles = meta["profiles"]
    if (
        meta["protocol"] != protocol
        or profiles not in (PROFILES, PROFILES[:2])
        or not rows
        or not len(rows) == len(captures) == meta["queries"] == done["expected"] == done["queries"]
        or len({r["query_id"] for r in rows}) != len(rows)
        or [r["query_id"] for r in rows] != [r["query_id"] for r in captures]
    ):
        raise ValueError("full paired capture and fixed protocol required")
    jobs, reuse = request_groups(captures, profiles)
    if (
        done["planned_calls"] != len(jobs)
        or done["observed_calls"] > len(jobs)
        or done["reused_outcomes"] > len(reuse)
    ):
        raise ValueError("physical/logical answer accounting differs")
    for i, row in enumerate(rows, 1):
        _validate_row(row, profiles=profiles, line_number=i)
        if row["selection"] != captures[i - 1]["selection"]:
            raise ValueError("selector accounting differs from capture")
    arms = {
        p: {
            "metrics": _metric_means(rows, p),
            "api": _api_summary(rows, p),
            "citation": _citation_summary(rows, p),
        }
        for p in profiles
    }
    kwargs = dict(group_key="group_id", seed=protocol["seed"], samples=5000, confidence=0.95)
    primary = paired_group_ci(rows, "token_f1", "legacy5", "set_rank5", **kwargs)
    order = (
        paired_group_ci(rows, "token_f1", "set_rank5", "set_read5", **kwargs)
        if "set_read5" in profiles
        else None
    )
    full = (
        paired_group_ci(rows, "token_f1", "legacy5", "set_read5", **kwargs)
        if "set_read5" in profiles
        else None
    )
    paired_tokens = []
    baseline_ms, pipeline_ms = [], []
    for row in rows:
        selection = row["selection"]
        base = row["arms"]["legacy5"]["api"]
        candidate = row["arms"]["set_rank5"]["api"]
        counts = (
            usage(base, "prompt_tokens"),
            usage(candidate, "prompt_tokens"),
            usage(selection, "prompt_tokens"),
        )
        if all(c is not None for c in counts):
            paired_tokens.append((counts[0], counts[1] + counts[2]))
        if base.get("request_ms") is not None and candidate.get("request_ms") is not None:
            baseline_ms.append(base["request_ms"])
            pipeline_ms.append(candidate["request_ms"] + selection["pipeline_ms"])
    token_ratio = (
        fmean(p[1] for p in paired_tokens) / fmean(p[0] for p in paired_tokens)
        if paired_tokens
        else None
    )
    latency_ratio = (
        _percentile(pipeline_ms, 0.95) / _percentile(baseline_ms, 0.95)
        if baseline_ms and pipeline_ms and _percentile(baseline_ms, 0.95)
        else None
    )
    cfg = protocol["stage2"]
    gates = {
        "run_complete": done["run_state"] == "COMPLETE"
        and done["observed_calls"] == len(jobs)
        and done["logical_outcomes"] == len(rows) * len(profiles),
        "structure_gate": structure["answer_stage_allowed"],
        "ordering_exact_fragments": all(c["ordering_fragments_equal"] for c in captures),
        "primary_f1_gain": primary["delta"] >= cfg["min_f1_gain"],
        "primary_positive_ci": primary["ci_low"] > cfg["ci_low_above"],
        "both_valid": min(arms[p]["api"]["valid_fraction"] for p in ("legacy5", "set_rank5"))
        >= cfg["min_valid_fraction"],
        "unknown_zero": all(arms[p]["api"]["unknown_outcome_count"] == 0 for p in profiles)
        and all(r["selection"]["status"] != "UNKNOWN_OUTCOME" for r in rows),
        "correct_model": all(
            a["api"]["status_counts"].get("MODEL_IDENTITY_ERROR", 0) == 0 for a in arms.values()
        )
        and all(r["selection"]["status"] != "MODEL_IDENTITY_ERROR" for r in rows),
        "recall_noninferior": arms["set_rank5"]["metrics"]["evidence_recall"]
        >= arms["legacy5"]["metrics"]["evidence_recall"] - cfg["max_recall_loss"],
        "usage_coverage": len(paired_tokens) / len(rows) >= cfg["minimum_usage_coverage"],
        "pipeline_input_budget": token_ratio is not None
        and token_ratio <= cfg["maximum_mean_pipeline_input_token_ratio"],
        "incremental_pipeline_p95": latency_ratio is not None
        and latency_ratio <= cfg["maximum_incremental_pipeline_p95_ratio"],
        "groups_valid": primary["inference_valid"],
    }
    physical_answers = [
        r["arms"][p]["api"]
        for r in rows
        for p in profiles
        if "reused_from" not in r["arms"][p]["api"]
    ]
    selections = [r["selection"] for r in rows]
    physical = physical_answers + selections
    observed = [usage(a, "total_tokens") for a in physical]
    return {
        "scope": __doc__,
        "queries": len(rows),
        "groups": len({r["group_id"] for r in rows}),
        "arms": arms,
        "primary_f1": primary,
        "reading_order_diagnostic": order,
        "full_reading_vs_baseline_diagnostic": full,
        "gates": gates,
        "passed": all(gates.values()),
        "selected_profile": "set_rank5" if all(gates.values()) else "legacy5",
        "pipeline": {
            "paired_usage_questions": len(paired_tokens),
            "mean_input_ratio": token_ratio,
            "p95_ratio": latency_ratio,
            "baseline_request_p95_ms": _percentile(baseline_ms, 0.95),
            "selector_plus_answer_p95_ms": _percentile(pipeline_ms, 0.95),
        },
        "physical_api": {
            "selections": len(selections),
            "answers": len(physical_answers),
            "answer_outcomes_reused": done["reused_outcomes"],
            "observed_total_tokens": sum(n for n in observed if n is not None),
            "unknown_usage_requests": sum(n is None for n in observed),
        },
        "strata": _hop_strata(rows, profiles),
        "limits": (
            "Exposed DEV/provided candidates/English short answers; selector overhead included "
            "only incremental model pipeline, not full live Run P95. ID validity is not semantic "
            "citation support. No price snapshot or independent confirmation inferred. "
            "Reading diagnostic cannot replace primary."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    meta = json.loads((args.run / "meta.json").read_text(encoding="utf-8"))
    done = json.loads((args.run / "completed.json").read_text(encoding="utf-8"))
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if args.frozen:
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if (
            frozen["protocol"] != protocol
            or frozen["selected_profile"] != "set_rank5"
            or not frozen["development_passed"]
            or meta["profiles"] != PROFILES[:2]
        ):
            raise ValueError("confirmation must preserve the accepted frozen primary")
    prepared = Path(meta["prepared"])
    captures = [
        json.loads(s)
        for s in (prepared / "prepared.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [
        json.loads(s) for s in (args.run / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    structure = json.loads((prepared / "structure.json").read_text(encoding="utf-8"))
    report = score(rows, captures, meta, done, protocol, structure)
    report["mode"] = "confirmation" if args.frozen else "development"
    if (args.run / "report.json").exists():
        raise ValueError("refusing existing report")
    dump(args.run / "report.json", report)
    if not args.frozen:
        dump(
            args.run / "frozen-selection.json",
            {
                "protocol": protocol,
                "selected_profile": report["selected_profile"],
                "development_passed": report["passed"],
            },
        )
    print(
        json.dumps({k: report[k] for k in ("queries", "passed", "primary_f1", "gates", "pipeline")})
    )


if __name__ == "__main__":
    main()
