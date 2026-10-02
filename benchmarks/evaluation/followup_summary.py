"""Combine fixed DEV observations without dropping interrupted or unreviewed work."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.assistant_review import review
from benchmarks.evaluation.interview_draft import build_draft
from packages.evaluation.quality_audit import TrialObservation, trial_metrics


def summarize(directory: Path):
    reviews = review(directory)
    names = ("live-dev-registration.json", "live-dev-registration-remaining.json")
    reports = [json.loads((directory / name).read_text(encoding="utf-8")) for name in names]
    for key in (
        "agent_version_id",
        "resolved_spec_hash",
        "dataset_hash",
        "model",
        "price_identity",
    ):
        if reports[0][key] != reports[1][key]:
            raise ValueError("INCOMPATIBLE_DEV_REPORTS")
    cases = [case for report in reports for case in report["cases"]]
    planned = [c for c in build_draft()["cases"] if c["split"] == "DEV"]
    identities = {(c["case_id"], c["repetition"]) for c in cases}
    if len(identities) != len(cases) or identities != {
        (case["case_id"], rep) for case in planned for rep in range(3)
    }:
        raise ValueError("INCOMPLETE_OR_DUPLICATE_PLAN")
    verdicts = {
        (entry["case_id"], entry["repetition"]): entry["verdict"]
        for entry in reviews["entries"]
        if entry["source_file"] in names
    }
    availability = {
        case["case_id"]: (
            False
            if case["category"] == "unanswerable"
            else True
            if case["category"] in {"single_hop", "multi_hop"}
            else None
        )
        for case in planned
    }
    rows = [
        TrialObservation(
            case_id=case["case_id"],
            variant="registration",
            repetition=case["repetition"],
            business_success=all(case["persistent_checks"].values())
            and verdicts[(case["case_id"], case["repetition"])] == "PASS",
            refused=(availability[case["case_id"]] is False)
            if availability[case["case_id"]] is not None
            else None,
            latency_ms=case["observation"]["latency_ms"],
        )
        for case in cases
    ]
    calls = [call for report in reports for call in report["calls"]]
    if len({c["attempt_id"] for c in calls}) != len(calls):
        raise ValueError("DUPLICATE_BILLING_ATTEMPT")
    known_cost = sum((Decimal(c["upper_estimated_cost_cny"]) for c in calls), Decimal(0))
    successes = sum(row.business_success for row in rows)
    result = {
        "formal_experiment": False,
        "reviewer_kind": "assistant",
        "source_reports": names,
        "dataset_hash": reports[0]["dataset_hash"],
        "agent_version_id": reports[0]["agent_version_id"],
        "resolved_spec_hash": reports[0]["resolved_spec_hash"],
        "observation_count": len(rows),
        "planned_case_count": len(planned),
        "metrics": trial_metrics(
            [c["case_id"] for c in planned], ["registration"], rows, answer_available=availability
        ),
        "refusal_scope": "informational single/multi-fact and unanswerable tasks only",
        "Q04": {
            "status": "NOT_AVAILABLE",
            "reason": "inline policies, no citation evidence identities",
        },
        "Q05": {"status": "NOT_AVAILABLE", "reason": "whole-output claim enumeration not frozen"},
        "Q08": reviews["Q08"],
        "P01": {"status": "NOT_AVAILABLE", "reason": "non-streaming execution"},
        "C01": {
            "currency": "CNY",
            "known_variant_cost_upper_estimate": str(known_cost),
            "business_successes": successes,
            "effective_task_cost_upper_estimate": str(known_cost / successes),
            "includes": (
                "all calls in both partial and remaining reports, including failed quality trials"
            ),
        },
        "C02": {
            "known_usage_attempts": len(calls),
            "known_price_attempts": len(calls),
            "recorded_attempts": len(calls),
            "scope": "this fixed variant only",
        },
        "failures": [
            {"case_id": e["case_id"], "repetition": e["repetition"], "reason": e["reason"]}
            for e in reviews["entries"]
            if e["source_file"] in names and e["verdict"] != "PASS"
        ],
        "scope": (
            "DEV trial with assistant review; "
            "not independent human benchmark or formal release gate"
        ),
    }
    write_json_atomic(directory / "dev-quality-summary.json", result)
    ledger = json.loads((directory / "live-budget.json").read_text(encoding="utf-8"))
    known = [a for a in ledger["attempts"] if a["status"] == "SETTLED_ESTIMATE"]
    unknown = [a for a in ledger["attempts"] if a["status"] != "SETTLED_ESTIMATE"]
    known_sum = sum((Decimal(a["charged_or_reserved_cny"]) for a in known), Decimal(0))
    unknown_sum = sum((Decimal(a["charged_or_reserved_cny"]) for a in unknown), Decimal(0))
    write_json_atomic(
        directory / "budget-summary.json",
        {
            "limit_cny": ledger["limit_cny"],
            "known_peak_upper_estimated_cny": str(known_sum),
            "unknown_reserved_cny": str(unknown_sum),
            "allocated_cny": str(known_sum + unknown_sum),
            "remaining_cny": str(Decimal(ledger["limit_cny"]) - known_sum - unknown_sum),
            "known_attempt_count": len(known),
            "unknown_attempt_count": len(unknown),
            "actual_invoice_amount_available": False,
        },
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    summarize(parser.parse_args().directory)
