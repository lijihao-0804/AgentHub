"""Report recorded public-RAG consumer answers without making API calls."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path
from statistics import fmean
from typing import Any

from benchmarks.public_rag.scenario_metrics import paired_group_ci

DATASET = "musique-ans"
LEGACY = "legacy5"
ALIGNED = "legacy5_aligned"
CANDIDATES = ("structured5", "structured10")
SELECTION_PROFILES = (LEGACY, *CANDIDATES)
DIAGNOSTIC_PROFILES = (ALIGNED, "no_rag", "gold_budgeted")
METRICS = (
    "token_f1",
    "exact_match",
    "evidence_recall",
    "complete_evidence_set",
    "full_text_evidence_recall",
    "final_evidence_bytes",
)
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
NON_ATTEMPTED = frozenset({"NOT_ATTEMPTED", "BUDGET_FALLBACK"})
RUN_STATES = frozenset({"COMPLETE", "NEEDS_ATTENTION"})


def score_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    meta: Mapping[str, Any],
    protocol: Mapping[str, Any],
    completed: Mapping[str, Any],
    frozen: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_protocol(protocol)
    _validate_capture(rows, meta=meta, protocol=protocol, completed=completed)
    profiles = list(meta["profiles"])
    if frozen is None:
        missing = set(SELECTION_PROFILES).difference(profiles)
        if missing:
            raise ValueError(f"development run is missing selection profiles: {sorted(missing)}")
        mode = "development"
        comparison_profiles = CANDIDATES
        confidence = float(protocol["development_selection"]["confidence"])
    else:
        frozen_profile = _validate_frozen(frozen, protocol)
        if frozen_profile == LEGACY:
            raise ValueError("frozen selection retains baseline; no candidate to confirm")
        if LEGACY not in profiles or frozen_profile not in profiles:
            raise ValueError("confirmation run is missing baseline or frozen profile")
        mode = "confirmation"
        comparison_profiles = (frozen_profile,)
        confidence = 0.95

    for line_number, row in enumerate(rows, start=1):
        _validate_row(row, profiles=profiles, line_number=line_number)
    arm_reports = {
        profile: {
            "questions": len(rows),
            "metrics": _metric_means(rows, profile),
            "api": _api_summary(rows, profile),
            "citation": _citation_summary(rows, profile),
        }
        for profile in profiles
    }
    comparisons = {
        profile: _compare_profile(
            rows,
            candidate=profile,
            protocol=protocol,
            completed=completed,
            confidence=confidence,
        )
        for profile in comparison_profiles
    }
    run_complete = completed["run_state"] == "COMPLETE"
    passed_profiles = [profile for profile in comparison_profiles if comparisons[profile]["passed"]]

    if mode == "development":
        if not run_complete:
            selected_profile = LEGACY
            quality_adoptable = False
            reason = "NEEDS_ATTENTION: quality adoption refused; baseline retained."
        elif "structured5" in passed_profiles:
            selected_profile = "structured5"
            quality_adoptable = True
            reason = "Both or structured5 only passed; structured5 has deterministic preference."
        elif "structured10" in passed_profiles:
            selected_profile = "structured10"
            quality_adoptable = True
            reason = "structured10 was the only passing candidate."
        else:
            selected_profile = LEGACY
            quality_adoptable = False
            reason = "No structured candidate passed; legacy5 retained."
        selection = {
            "mode": mode,
            "selected_profile": selected_profile,
            "passed_candidates": passed_profiles,
            "quality_adoptable": quality_adoptable,
            "reason": reason,
        }
        confirmation = None
    else:
        frozen_profile = _validate_frozen(frozen, protocol)
        passed = comparisons[frozen_profile]["passed"]
        selection = {
            "mode": mode,
            "selected_profile": frozen_profile,
            "quality_adoptable": passed,
            "reason": (
                "Frozen profile passed confirmation gates."
                if passed
                else "Frozen profile failed confirmation; no reselection performed."
            ),
        }
        confirmation = {
            "frozen_profile": frozen_profile,
            "compared_with": LEGACY,
            "passed": passed,
            "no_reselection": True,
            "confidence": confidence,
        }

    return {
        "dataset": DATASET,
        "protocol": dict(protocol),
        "run_state": completed["run_state"],
        "prepared": meta["prepared"],
        "prior_run": meta.get("prior_run"),
        "profiles": profiles,
        "selection_profiles": list(SELECTION_PROFILES),
        "denominators": {
            "planned_questions": int(meta["queries"]),
            "captured_questions": len(rows),
            "unique_query_ids": len({row["query_id"] for row in rows}),
            "groups": len({row["group_id"] for row in rows}),
        },
        "arms": arm_reports,
        "comparisons": comparisons,
        "aligned_diagnostic": _aligned_diagnostic(rows, profiles),
        "hop_strata": _hop_strata(rows, profiles),
        "selection": selection,
        "selected_profile": selection["selected_profile"],
        "confirmation": confirmation,
        "limitations": [
            "Token F1 and normalized exact match are short-answer metrics.",
            "Citation metrics validate admitted chunk IDs only; semantic support and "
            "faithfulness are not evaluated.",
            "This is a controlled cached-candidate consumer evaluation, not live SQL/Qdrant, "
            "tenant-isolation, checkpoint, or full API E2E evidence.",
            "no_rag and gold_budgeted are diagnostics and never participate in method selection.",
            "Hop strata are descriptive only and are not used for routing or inference.",
            "Provider cost is not inferred because no pricing snapshot is bound.",
        ],
    }


def build_report(
    run: Path, *, protocol_path: Path, frozen_path: Path | None = None
) -> dict[str, Any]:
    run = Path(run)
    report_path = run / "report.json"
    development_frozen_path = run / "frozen-selection.json"
    if report_path.exists():
        raise ValueError("refusing to overwrite existing report.json")
    if frozen_path is None and development_frozen_path.exists():
        raise ValueError("refusing to overwrite existing frozen-selection.json")
    protocol = _read_json(Path(protocol_path))
    meta = _read_json(run / "meta.json")
    completed = _read_json(run / "completed.json")
    rows = _read_jsonl(run / "run.jsonl")
    frozen = _read_json(Path(frozen_path)) if frozen_path is not None else None
    report = score_rows(rows, meta=meta, protocol=protocol, completed=completed, frozen=frozen)
    if frozen is None:
        frozen_payload = {
            "protocol": protocol,
            "dataset": DATASET,
            "selected_profile": report["selected_profile"],
            "development_run": run.as_posix(),
            "quality_adoptable": report["selection"]["quality_adoptable"],
            "selection": report["selection"],
        }
        report["development_run"] = run.as_posix()
        _write_new_json(report_path, report)
        _write_new_json(development_frozen_path, frozen_payload)
    else:
        report["development_run"] = frozen["development_run"]
        _write_new_json(report_path, report)
    return report


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("dataset") != DATASET:
        raise ValueError(f"protocol dataset must be {DATASET!r}")
    if type(protocol.get("seed")) is not int:
        raise ValueError("protocol seed must be an integer")
    selection = protocol.get("development_selection")
    if not isinstance(selection, Mapping):
        raise ValueError("protocol requires development_selection")
    if selection.get("alternatives") != list(CANDIDATES):
        raise ValueError("development alternatives differ from structured5/structured10")
    if selection.get("versus") != LEGACY:
        raise ValueError("development comparison must be versus legacy5")
    if selection.get("grouped_bootstrap_samples") != 5000:
        raise ValueError("development bootstrap must use 5000 samples")
    if selection.get("confidence") != 0.975:
        raise ValueError("development bootstrap confidence must be 0.975")
    if selection.get("minimum_answer_f1_gain") != 0.03:
        raise ValueError("development minimum F1 gain must be 0.03")
    if selection.get("max_record_recall_loss") != 0.01:
        raise ValueError("development maximum evidence recall loss must be 0.01")
    if selection.get("min_valid_fraction") != 0.95:
        raise ValueError("development valid fraction must be 0.95")
    if selection.get("unknown_outcomes") != 0:
        raise ValueError("development unknown-outcome limit must be zero")
    if selection.get("ci_lower_above_zero") is not True:
        raise ValueError("development protocol must require CI lower bound above zero")


def _validate_capture(
    rows: Sequence[Mapping[str, Any]],
    *,
    meta: Mapping[str, Any],
    protocol: Mapping[str, Any],
    completed: Mapping[str, Any],
) -> None:
    if meta.get("protocol") != protocol:
        raise ValueError("run metadata protocol differs from scoring protocol")
    prepared = meta.get("prepared")
    if not isinstance(prepared, str) or not prepared:
        raise ValueError("run metadata requires prepared path")
    profiles = meta.get("profiles")
    if (
        not isinstance(profiles, list)
        or not profiles
        or any(not isinstance(profile, str) or not profile for profile in profiles)
        or len(set(profiles)) != len(profiles)
    ):
        raise ValueError("run metadata profiles must be unique non-empty strings")
    unknown_profiles = set(profiles).difference({*SELECTION_PROFILES, *DIAGNOSTIC_PROFILES})
    if unknown_profiles:
        raise ValueError(f"run metadata has unknown profiles: {sorted(unknown_profiles)}")
    planned = _positive_int(meta.get("queries"), "meta.queries")
    expected = _positive_int(completed.get("expected"), "completed.expected")
    captured = _positive_int(completed.get("queries"), "completed.queries")
    run_state = completed.get("run_state")
    if run_state not in RUN_STATES:
        raise ValueError("completed.run_state must be COMPLETE or NEEDS_ATTENTION")
    if expected != planned or captured != planned or len(rows) != planned:
        raise ValueError(
            "full planned capture required: "
            f"expected={expected}, completed={captured}, metadata={planned}, rows={len(rows)}"
        )
    query_ids = [row.get("query_id") for row in rows]
    if any(not isinstance(query_id, str) or not query_id for query_id in query_ids):
        raise ValueError("every run row requires a non-empty query_id")
    if len(set(query_ids)) != len(query_ids):
        raise ValueError("duplicate query_id in run capture")


def _validate_frozen(frozen: Mapping[str, Any], protocol: Mapping[str, Any]) -> str:
    if not isinstance(frozen, Mapping):
        raise ValueError("frozen selection must be a JSON object")
    if frozen.get("protocol") != protocol:
        raise ValueError("frozen selection protocol differs")
    if frozen.get("dataset") != DATASET:
        raise ValueError(f"frozen selection dataset must be {DATASET!r}")
    profile = frozen.get("selected_profile")
    if profile not in CANDIDATES:
        raise ValueError("frozen selection must name a structured candidate")
    if not isinstance(frozen.get("development_run"), str) or not frozen["development_run"]:
        raise ValueError("frozen selection requires development_run")
    return profile


def _validate_row(row: Mapping[str, Any], *, profiles: Sequence[str], line_number: int) -> None:
    prefix = f"run.jsonl line {line_number}"
    if not isinstance(row.get("group_id"), str) or not row["group_id"]:
        raise ValueError(f"{prefix} requires a non-empty group_id")
    _positive_int(row.get("hops"), f"{prefix} hops")
    arms = row.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != set(profiles):
        raise ValueError(f"{prefix} arms must exactly match meta.profiles")
    for profile in profiles:
        arm = arms[profile]
        if not isinstance(arm, Mapping):
            raise ValueError(f"{prefix} {profile} arm must be an object")
        for metric in ("token_f1", "evidence_recall", "full_text_evidence_recall"):
            _fraction(arm.get(metric), f"{prefix} {profile}.{metric}")
        for metric in ("exact_match", "complete_evidence_set"):
            if type(arm.get(metric)) is not int or arm[metric] not in (0, 1):
                raise ValueError(f"{prefix} {profile}.{metric} must be 0 or 1")
        _nonnegative_int(
            arm.get("final_evidence_bytes"), f"{prefix} {profile}.final_evidence_bytes"
        )
        _validate_api(arm.get("api"), f"{prefix} {profile}.api")
        _validate_citation(arm.get("citation"), f"{prefix} {profile}.citation")
        if arm["api"]["status"] != "VALID" and (arm["token_f1"] != 0 or arm["exact_match"] != 0):
            raise ValueError(f"{prefix} {profile} failed answer must retain zero answer metrics")


def _validate_api(value: object, name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    status = value.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError(f"{name}.status must be a non-empty string")
    usage = value.get("usage")
    if usage is not None:
        if not isinstance(usage, Mapping):
            raise ValueError(f"{name}.usage must be an object or null")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
            if key in usage:
                _nonnegative_int(usage[key], f"{name}.usage.{key}")
        details = usage.get("completion_tokens_details")
        if details is not None:
            if not isinstance(details, Mapping):
                raise ValueError(f"{name}.usage.completion_tokens_details must be an object")
            if "reasoning_tokens" in details:
                _nonnegative_int(
                    details["reasoning_tokens"],
                    f"{name}.usage.completion_tokens_details.reasoning_tokens",
                )
    request_ms = value.get("request_ms")
    if status in NON_ATTEMPTED:
        if request_ms is not None:
            _nonnegative_number(request_ms, f"{name}.request_ms")
    else:
        _nonnegative_number(request_ms, f"{name}.request_ms")


def _validate_citation(value: object, name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    _fraction(value.get("validity"), f"{name}.validity")
    _nonnegative_int(value.get("unknown"), f"{name}.unknown")
    _nonnegative_int(value.get("duplicates"), f"{name}.duplicates")
    if type(value.get("has_citations")) is not bool:
        raise ValueError(f"{name}.has_citations must be boolean")


def _compare_profile(
    rows: Sequence[Mapping[str, Any]],
    *,
    candidate: str,
    protocol: Mapping[str, Any],
    completed: Mapping[str, Any],
    confidence: float,
) -> dict[str, Any]:
    selection = protocol["development_selection"]
    samples = int(selection["grouped_bootstrap_samples"])
    baseline_mean = fmean(row["arms"]["legacy5"]["token_f1"] for row in rows)
    candidate_mean = fmean(row["arms"][candidate]["token_f1"] for row in rows)
    gain = candidate_mean - baseline_mean
    recall_loss = fmean(
        row["arms"]["legacy5"]["evidence_recall"] - row["arms"][candidate]["evidence_recall"]
        for row in rows
    )
    ci = paired_group_ci(
        list(rows),
        "token_f1",
        "legacy5",
        candidate,
        group_key="group_id",
        samples=samples,
        confidence=confidence,
        seed=int(protocol["seed"]),
    )
    candidate_api = _api_summary(rows, candidate)
    baseline_api = _api_summary(rows, LEGACY)
    minimum_gain = float(selection["minimum_answer_f1_gain"])
    minimum_valid = float(selection["min_valid_fraction"])
    maximum_recall_loss = float(selection["max_record_recall_loss"])
    unknown_limit = int(selection["unknown_outcomes"])
    gates = {}
    gates["full_planned_capture"] = {
        "passed": len(rows) == int(completed["expected"]) == int(completed["queries"])
    }
    gates["run_state_complete"] = {
        "passed": completed["run_state"] == "COMPLETE",
        "observed": completed["run_state"],
    }
    gates["minimum_f1_gain"] = {
        "passed": gain >= minimum_gain,
        "observed": gain,
        "threshold": minimum_gain,
    }
    gates["ci_lower_above_zero"] = {
        "passed": ci["inference_valid"] and ci["ci_low"] > 0,
        "observed": ci["ci_low"],
        "confidence": confidence,
    }
    gates["valid_api_fraction"] = {
        "passed": candidate_api["valid_fraction"] >= minimum_valid,
        "observed": candidate_api["valid_fraction"],
        "threshold": minimum_valid,
    }
    gates["baseline_valid_api_fraction"] = {
        "passed": baseline_api["valid_fraction"] >= minimum_valid,
        "observed": baseline_api["valid_fraction"],
        "threshold": minimum_valid,
    }
    gates["unknown_outcomes_zero"] = {
        "passed": candidate_api["unknown_outcome_count"] + baseline_api["unknown_outcome_count"]
        <= unknown_limit,
        "observed": candidate_api["unknown_outcome_count"] + baseline_api["unknown_outcome_count"],
        "maximum": unknown_limit,
    }
    gates["recall_loss_within_limit"] = {
        "passed": recall_loss <= maximum_recall_loss,
        "observed": recall_loss,
        "maximum": maximum_recall_loss,
    }
    return {
        "candidate_profile": candidate,
        "baseline_profile": "legacy5",
        "metric": "token_f1",
        "baseline_mean": baseline_mean,
        "candidate_mean": candidate_mean,
        "gain": gain,
        "recall_loss": recall_loss,
        "ci": {
            "method": "paired group bootstrap",
            "group_key": "group_id",
            "samples": samples,
            "confidence": confidence,
            "difference": ci["difference"],
            "low": ci["ci_low"],
            "high": ci["ci_high"],
            "groups": ci["groups"],
            "queries": ci["queries"],
            "inference_valid": ci["inference_valid"],
        },
        "gates": gates,
        "engineering_checks": {
            "adequate_questions": len(rows) >= 100,
            "adequate_groups": len({row["group_id"] for row in rows}) >= 20,
            "used_for_method_selection": False,
        },
        "passed": all(gate["passed"] for gate in gates.values()),
    }


def _aligned_diagnostic(
    rows: Sequence[Mapping[str, Any]], profiles: Sequence[str]
) -> dict[str, Any]:
    required = {"legacy5_aligned", "structured5"}
    if not required.issubset(profiles):
        return {
            "status": "SKIPPED_MISSING_PROFILE",
            "required_profiles": sorted(required),
            "used_for_selection": False,
            "significance_test": False,
        }
    structured_f1 = fmean(row["arms"]["structured5"]["token_f1"] for row in rows)
    aligned_f1 = fmean(row["arms"]["legacy5_aligned"]["token_f1"] for row in rows)
    structured_recall = fmean(row["arms"]["structured5"]["evidence_recall"] for row in rows)
    aligned_recall = fmean(row["arms"]["legacy5_aligned"]["evidence_recall"] for row in rows)
    return {
        "status": "AVAILABLE",
        "candidate_profile": "structured5",
        "diagnostic_profile": "legacy5_aligned",
        "token_f1_difference": structured_f1 - aligned_f1,
        "record_recall_difference": structured_recall - aligned_recall,
        "direction": "structured5 minus legacy5_aligned",
        "used_for_selection": False,
        "significance_test": False,
        "interpretation": "Descriptive separation of projection/structure from larger input "
        "budgets; not evidence that the structured method caused the whole difference.",
    }


def _metric_means(rows: Sequence[Mapping[str, Any]], profile: str) -> dict[str, float]:
    return {metric: fmean(row["arms"][profile][metric] for row in rows) for metric in METRICS}


def _api_summary(rows: Sequence[Mapping[str, Any]], profile: str) -> dict[str, Any]:
    statuses = Counter(row["arms"][profile]["api"]["status"] for row in rows)
    attempted = [
        row["arms"][profile]["api"]
        for row in rows
        if row["arms"][profile]["api"]["status"] not in NON_ATTEMPTED
    ]
    latencies = [float(api["request_ms"]) for api in attempted if api["request_ms"] is not None]
    usage = {}
    for key in TOKEN_FIELDS:
        observed = [
            value for api in attempted if (value := _usage_token(api["usage"], key)) is not None
        ]
        usage[key] = {
            "sum": sum(observed) if observed else None,
            "observed_count": len(observed),
            "unknown_count": len(attempted) - len(observed),
        }
    return {
        "status_counts": dict(statuses),
        "attempted_count": len(attempted),
        "not_attempted_count": len(rows) - len(attempted),
        "valid_count": statuses["VALID"],
        "valid_fraction": statuses["VALID"] / len(rows),
        "unknown_outcome_count": statuses["UNKNOWN_OUTCOME"],
        "request_latency_ms": {
            "n": len(latencies),
            "p50": _quantile(latencies, 0.50),
            "p95": _quantile(latencies, 0.95),
            "basis": "Incremental provider request only; excludes retrieval, runtime, and "
            "overall consumer flow.",
        },
        "usage": usage,
        "usage_note": "Observed provider usage only; missing values remain unknown and no "
        "pricing or currency cost is inferred.",
    }


def _citation_summary(rows: Sequence[Mapping[str, Any]], profile: str) -> dict[str, Any]:
    citations = [row["arms"][profile]["citation"] for row in rows]
    return {
        "mean_id_validity": fmean(citation["validity"] for citation in citations),
        "unknown_id_count": sum(citation["unknown"] for citation in citations),
        "duplicate_id_count": sum(citation["duplicates"] for citation in citations),
        "responses_with_citations": sum(citation["has_citations"] for citation in citations),
        "scope": "Admitted chunk-ID validity only; semantic support and faithfulness are "
        "not evaluated.",
    }


def _hop_strata(rows: Sequence[Mapping[str, Any]], profiles: Sequence[str]) -> dict[str, Any]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row["hops"])].append(row)
    by_hops = {}
    for hops, group in sorted(grouped.items()):
        by_hops[str(hops)] = {
            "questions": len(group),
            "profiles": {profile: _metric_means(group, profile) for profile in profiles},
        }
    return {
        "scope": "Descriptive only; no significance test, model routing, or hop-conditioned "
        "method selection.",
        "by_hops": by_hops,
    }


def _usage_token(usage: object, key: str) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(key)
    if type(value) is int and value >= 0:
        return value
    if key == "reasoning_tokens":
        details = usage.get("completion_tokens_details")
        if isinstance(details, Mapping):
            value = details.get(key)
            if type(value) is int and value >= 0:
                return value
    return None


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _fraction(value: object, name: str) -> float:
    number = _nonnegative_number(value, name)
    if number > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read run trace: {path}") from exc
    rows = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"{path.name} line {line_number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {line_number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {line_number} must be a JSON object")
        rows.append(row)
    return rows


def _write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise ValueError(f"refusing to overwrite {path}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    report = build_report(args.run, protocol_path=args.protocol, frozen_path=args.frozen)
    print(
        json.dumps(
            {
                "report": (args.run / "report.json").as_posix(),
                "selected_profile": report["selected_profile"],
                "quality_adoptable": report["selection"]["quality_adoptable"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

__all__ = ["build_report", "score_rows"]
