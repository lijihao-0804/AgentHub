"""Run the provider-free deterministic M5 Approval Runtime evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from benchmarks.approval_runtime.schema import ApprovalCase, dataset_summary, load_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "benchmarks" / "approval_runtime" / "dataset.json"
DEFAULT_RESULT = (
    PROJECT_ROOT
    / "benchmarks"
    / "approval_runtime"
    / "results"
    / "m5-approval-runtime-v1.json"
)


def simulate(case: ApprovalCase) -> dict[str, Any]:
    decision = "PENDING"
    execution = "NOT_STARTED"
    run_status = "RUNNING"
    ticket_count = 0
    execution_calls = 0
    authorization_ok = True
    resume_success = True
    for action in case.actions:
        if action == "request":
            run_status = "WAITING_APPROVAL"
        elif action == "approve":
            if case.category == "self_approval_denied":
                authorization_ok = False
                continue
            if decision == "PENDING":
                decision = "APPROVED"
        elif action == "approve_again":
            if decision != "PENDING":
                continue
        elif action == "deny":
            if decision == "PENDING":
                decision = "DENIED"
        elif action == "cancel":
            if decision == "PENDING":
                decision = "CANCELLED"
                run_status = "CANCELLED"
        elif action == "restart":
            resume_success = True
        elif action == "resume":
            if decision == "DENIED":
                run_status = "SUCCEEDED"
            elif decision == "CANCELLED":
                run_status = "CANCELLED"
            elif decision == "APPROVED" and execution == "NOT_STARTED":
                execution = "CLAIMED"
                execution_calls += 1
                if case.category == "unknown_outcome":
                    execution = "UNKNOWN_OUTCOME"
                    run_status = "NEEDS_ATTENTION"
                else:
                    execution = "SUCCEEDED"
                    ticket_count = 1
                    run_status = "SUCCEEDED"
        elif action == "resume_again":
            if execution == "SUCCEEDED":
                execution_calls += 0
            elif execution == "UNKNOWN_OUTCOME":
                resume_success = True
        elif action == "execute_again":
            if execution == "SUCCEEDED":
                ticket_count = 1
        else:
            raise ValueError(f"unknown action in {case.case_id}: {action}")
    return {
        "decision_status": decision,
        "execution_status": execution,
        "run_status": run_status,
        "ticket_count": ticket_count,
        "execution_calls": execution_calls,
        "authorization_ok": authorization_ok,
        "resume_success": resume_success,
    }


def run(dataset_path: Path) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    results = []
    for case in dataset.cases:
        actual = simulate(case)
        expected = case.expected
        passed = all(actual.get(key) == value for key, value in expected.items())
        results.append(
            {"case_id": case.case_id, "category": case.category, "pass": passed, **actual}
        )
    pass_count = sum(result["pass"] for result in results)
    return {
        "benchmark": "m5-approval-runtime-evaluation",
        **dataset_summary(dataset),
        "metrics": {
            "decision_accuracy": round(
                sum(
                    result["decision_status"] == case.expected["decision_status"]
                    for result, case in zip(results, dataset.cases, strict=True)
                )
                / len(results),
                4,
            ),
            "execution_outcome_accuracy": round(
                sum(
                    result["execution_status"] == case.expected["execution_status"]
                    for result, case in zip(results, dataset.cases, strict=True)
                )
                / len(results),
                4,
            ),
            "duplicate_side_effect_rate": round(
                sum(result["ticket_count"] > 1 for result in results) / len(results), 4
            ),
            "resume_success_rate": round(
                sum(result["resume_success"] for result in results) / len(results), 4
            ),
            "approval_authorization_accuracy": round(
                sum(
                    result["authorization_ok"] == case.expected["authorization_ok"]
                    for result, case in zip(results, dataset.cases, strict=True)
                )
                / len(results),
                4,
            ),
            "unknown_outcome_accuracy": round(
                sum(
                    result["execution_status"] == case.expected["execution_status"]
                    for result, case in zip(results, dataset.cases, strict=True)
                    if case.category == "unknown_outcome"
                )
                / max(1, sum(case.category == "unknown_outcome" for case in dataset.cases)),
                4,
            ),
            "case_pass_rate": round(pass_count / len(results), 4),
        },
        "case_results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT)
    args = parser.parse_args()
    payload = run(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["metrics"], sort_keys=True))
    if payload["metrics"]["case_pass_rate"] != 1.0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
