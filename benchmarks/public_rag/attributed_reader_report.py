"""One preregistered reader comparison; source alignment is not semantic faithfulness."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.attributed_reader_runner import PROFILES
from benchmarks.public_rag.consumer_report import (
    _api_summary,
    _citation_summary,
    _hop_strata,
    _metric_means,
    _validate_row,
)
from benchmarks.public_rag.scenario_metrics import _percentile, paired_group_ci
from benchmarks.public_rag.scenario_runner import dump

CANDIDATE, CONTROL = "attribute512_answer256", "direct768"


def input_total(arm):
    calls = list(arm["calls"].values())
    if arm["api"].get("reused_from_prior256"):
        calls.append(arm["api"])
    values = []
    for call in calls:
        usage = call.get("usage")
        value = usage.get("prompt_tokens") if type(usage) is dict else None
        if type(value) is not int or value < 0:
            return None
        values.append(value)
    return sum(values) if values else None


def pipeline_ms(arm):
    value = arm["pipeline_ms"]
    if value is None:
        return None
    if arm["api"].get("reused_from_prior256"):
        prior = arm["api"].get("request_ms")
        if prior is None:
            return None
        value += prior
    return value


def actual_payload_bytes(arm):
    messages = arm.get("answer_messages")
    if messages is None:
        return None
    return sum(len(m["content"].encode("utf-8")) for m in messages if m["role"] == "tool")


def score(rows, meta, done, protocol):
    if (
        not rows
        or meta["protocol"] != protocol
        or meta["profiles"] != PROFILES
        or not len(rows) == meta["queries"] == done["expected"] == done["queries"]
        or len({r["query_id"] for r in rows}) != len(rows)
    ):
        raise ValueError("full fixed reader capture required")
    for i, row in enumerate(rows, 1):
        _validate_row(row, profiles=PROFILES, line_number=i)
    arms = {
        p: {
            "metrics": _metric_means(rows, p),
            "api": _api_summary(rows, p),
            "citation": _citation_summary(rows, p),
        }
        for p in PROFILES
    }
    cfg = protocol["gates"]
    comparison = paired_group_ci(
        rows,
        "token_f1",
        CONTROL,
        CANDIDATE,
        group_key="group_id",
        seed=protocol["seed"],
        samples=cfg["bootstrap_samples"],
        confidence=cfg["confidence"],
    )
    pairs = []
    for row in rows:
        a, b = (input_total(row["arms"][p]) for p in (CONTROL, CANDIDATE))
        if a is not None and b is not None:
            pairs.append((a, b))
    ratio = fmean(b for _, b in pairs) / fmean(a for a, _ in pairs) if pairs else None
    latency = {
        p: [v for r in rows if (v := pipeline_ms(r["arms"][p])) is not None]
        for p in (CONTROL, CANDIDATE)
    }
    p95 = {p: _percentile(v, 0.95) if v else None for p, v in latency.items()}
    p95_ratio = (
        p95[CANDIDATE] / p95[CONTROL] if p95[CONTROL] and p95[CANDIDATE] is not None else None
    )
    statuses = Counter(r["arms"][CANDIDATE]["attribution_status"] for r in rows)
    physical = [call for row in rows for p in PROFILES for call in row["arms"][p]["calls"].values()]
    api_statuses = Counter(c["status"] for c in physical)
    all_valid_quotes = sum(
        status == "VALID"
        for row in rows
        if (status := row["arms"][CANDIDATE]["attribution_status"])
    )
    gates = {
        "complete": done["run_state"] == "COMPLETE" and done["completed_profiles"] == len(rows) * 2,
        "physical_count": done["physical_calls"] == len(physical),
        "f1_minimum_gain": comparison["delta"] >= cfg["minimum_f1_gain"],
        "f1_positive_group_ci": comparison["ci_low"] > cfg["paired95_ci_low_above"],
        "em_noninferior": arms[CANDIDATE]["metrics"]["exact_match"]
        >= arms[CONTROL]["metrics"]["exact_match"] - cfg["maximum_em_loss"],
        "both_valid": min(arms[p]["api"]["valid_fraction"] for p in (CONTROL, CANDIDATE))
        >= cfg["min_valid_each"],
        "attribution_valid": all_valid_quotes / len(rows) >= cfg["min_valid_attribution"],
        "unknown_zero": api_statuses.get("UNKNOWN_OUTCOME", 0) == cfg["unknown_outcomes"],
        "correct_model": api_statuses.get("MODEL_IDENTITY_ERROR", 0) == 0,
        "original_sources_unchanged": all(
            r["arms"][p]["source_fragments_unchanged"] for r in rows for p in PROFILES
        ),
        "candidate_citations_known": arms[CANDIDATE]["citation"]["unknown_id_count"]
        == cfg["candidate_unknown_citation_ids"],
        "paired_usage_coverage": len(pairs) / len(rows) >= cfg["minimum_paired_usage_coverage"],
        "input_overhead": ratio is not None and ratio <= cfg["maximum_mean_pipeline_input_ratio"],
        "pipeline_p95": p95_ratio is not None and p95_ratio <= cfg["maximum_pipeline_p95_ratio"],
        "inference_valid": comparison["inference_valid"],
    }
    usage_values = [
        c["usage"]["total_tokens"]
        for c in physical
        if isinstance(c.get("usage"), dict) and type(c["usage"].get("total_tokens")) is int
    ]
    payload = {
        p: [v for r in rows if (v := actual_payload_bytes(r["arms"][p])) is not None]
        for p in PROFILES
    }
    full = [r for r in rows if r["arms"][CANDIDATE]["complete_evidence_set"] == 1]
    return {
        "scope": __doc__,
        "queries": len(rows),
        "groups": len({r["group_id"] for r in rows}),
        "arms": arms,
        "primary_f1": comparison,
        "gates": gates,
        "passed": all(gates.values()),
        "selected_profile": CANDIDATE if all(gates.values()) else "direct256_prior",
        "attribution_statuses": dict(statuses),
        "pipeline": {
            "paired_usage_questions": len(pairs),
            "mean_input_ratio": ratio,
            "p95_ms": p95,
            "p95_ratio": p95_ratio,
            "fallback_basis": (
                "historical direct256 cost/time included for logical deployment pipeline; "
                "excluded from new paid requests"
            ),
        },
        "actual_answer_tool_payload_bytes": {
            p: {"observed": len(v), "mean": fmean(v) if v else None} for p, v in payload.items()
        },
        "physical_api": {
            "requests": len(physical),
            "statuses": dict(api_statuses),
            "observed_total_tokens": sum(usage_values),
            "usage_observed": len(usage_values),
            "usage_unknown": len(physical) - len(usage_values),
            "currency_cost": None,
            "historical_control_reuses": done["prior_control_reuses"],
            "fallback_reuses": done["candidate_fallback_reuses"],
        },
        "complete_paragraph_support_subset": {
            "questions": len(full),
            "exact_match_by_profile": {
                p: sum(r["arms"][p]["exact_match"] for r in full) for p in PROFILES
            },
            "scope": (
                "descriptive, paragraph-ID completeness does not guarantee gold-fact span coverage"
            ),
        },
        "strata": _hop_strata(rows, PROFILES),
        "limits": [
            "Exposed English provided-passage DEV, frozen upstream evidence; "
            "not full Run/auth/checkpoint E2E",
            "Exact span validation proves source existence only; "
            "not relevance/entailment or complete reasoning",
            "Equal maximum output caps are not matched actual compute; "
            "actual total model input/latency reported",
            "Raw final_evidence_bytes is original source footprint; "
            "annotated actual tool bytes reported separately",
            "Direct256 is historical exact-input diagnostic; "
            "only live direct768 is the primary comparator",
            "No unanswerable negative dataset or human-calibrated semantic citation score; "
            "no monetary pricing snapshot",
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    args = p.parse_args()
    rows = [
        json.loads(s) for s in (args.run / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    meta = json.loads((args.run / "meta.json").read_text(encoding="utf-8"))
    done = json.loads((args.run / "completed.json").read_text(encoding="utf-8"))
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if (args.run / "report.json").exists():
        raise ValueError("refusing previous reader score")
    report = score(rows, meta, done, protocol)
    dump(args.run / "report.json", report)
    dump(
        args.run / "frozen-selection.json",
        {
            "protocol": protocol,
            "selected_profile": report["selected_profile"],
            "development_passed": report["passed"],
        },
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "queries",
                    "passed",
                    "primary_f1",
                    "gates",
                    "attribution_statuses",
                    "pipeline",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
