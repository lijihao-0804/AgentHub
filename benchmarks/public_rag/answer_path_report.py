"""Pure scoring for the registered paper answer-path diagnostic.

The report keeps answer correctness separate from citation validity and from
the construction of evidence conditions. It performs no model, network,
filesystem, or semantic entailment work.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Any

from benchmarks.public_rag.consumer_answer_metrics import answer_metrics
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.scifact_report import quantile

CONDITIONS = ("actual", "sufficient", "removed")
RESULT_STATUSES = frozenset(
    {
        "VALID",
        "INVALID_OUTPUT",
        "PROVIDER_ERROR",
        "UNKNOWN_OUTCOME",
        "NOT_ATTEMPTED",
    }
)
REQUIRED_RESULT_FIELDS = frozenset(
    {"status", "answer", "unanswerable", "cited_source_ids", "usage", "latency_ms"}
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def score_diagnostic(dataset: Mapping[str, Any], rows: Mapping[str, Any]) -> dict[str, Any]:
    """Score every registered case and preserve failures in fixed denominators.

    ``rows`` must be the runner's explicit object with ``cases`` and
    ``natural_controls`` arrays. Each array must contain the same ordered
    query/paper identities as the frozen dataset. A missing or extra arm marks
    that registered case as rejected and scores the absent arm as zero.
    """
    cases = _dataset_cases(dataset)
    controls = _dataset_controls(dataset)
    case_rows, control_rows = _row_strata(rows)
    row_by_id = _row_index(case_rows, cases, stratum="cases")
    control_row_by_id = _row_index(control_rows, controls, stratum="natural_controls")

    evaluated_cases = [_score_case(case, row_by_id[case["query_id"]]) for case in cases]
    evaluated_controls = [
        _score_control(control, control_row_by_id[control["query_id"]]) for control in controls
    ]
    all_results = [result for case in evaluated_cases for result in case["conditions"].values()] + [
        control["result"] for control in evaluated_controls
    ]

    ledger = dataset.get("ledger", [])
    if not isinstance(ledger, list) or any(not isinstance(item, Mapping) for item in ledger):
        raise ValueError("dataset ledger must be a list of objects")
    ledger_audit = _ledger_audit(ledger)
    main_planned = len(cases) * len(CONDITIONS)
    control_planned = len(controls)
    valid_count = sum(result["valid"] for result in all_results)

    return {
        "scope": (
            "Frozen QASPER TRAIN answer-path development diagnostic; "
            "descriptive only, with no semantic entailment score or method promotion."
        ),
        "population": {
            "active_cases": len(cases),
            "natural_controls": len(controls),
            "main_planned_requests": main_planned,
            "control_planned_requests": control_planned,
            "planned_requests": main_planned + control_planned,
            "valid_results": valid_count,
            "invalid_or_error_results": len(all_results) - valid_count,
            "missing_or_rejected_rows": sum(case["row_rejected"] for case in evaluated_cases)
            + sum(control["row_rejected"] for control in evaluated_controls),
            "ledger_cases": len(ledger),
            **ledger_audit,
        },
        "identifier_metrics": _identifier_metrics(all_results),
        "answer_path_main": _main_report(evaluated_cases),
        "natural_controls": _control_report(evaluated_controls),
        "paper_cluster_diagnostics": _paper_cluster_diagnostics(evaluated_cases),
        "usage": _usage_report(all_results),
        "cost": None,
        "limitations": [
            "Official max-reference exact-match and token F1 measure answer fact overlap.",
            "Citation IDs are checked only for uniqueness and visibility; validity does not "
            "measure semantic support or entailment.",
            "Removed-condition gold-answer overlap receives zero credit; only a complete "
            "source-only refusal counts as correct.",
            "Constructed negatives and natural human-unanswerable controls remain separate "
            "and are never added to answerable F1 denominators.",
            "Paper-cluster intervals are descriptive diagnostics, not significance tests "
            "or evidence for method promotion.",
            "Semantic support requires later external annotation; no semantic score is emitted.",
        ],
    }


def _dataset_cases(dataset: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if dataset.get("schema_version") != 1 or dataset.get("state") != "FROZEN_DEVELOPMENT":
        raise ValueError("one frozen development diagnostic dataset is required")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset cases must be a nonempty list")
    seen: set[str] = set()
    for case in cases:
        _require_mapping(case, "case")
        query_id = _identifier(case.get("query_id"), "case query_id")
        _identifier(case.get("paper_id"), f"{query_id} paper_id")
        _nonempty_string(case.get("question"), f"{query_id} question")
        references = case.get("answer_references")
        if (
            not isinstance(references, list)
            or not references
            or any(type(item) is not str or not item.strip() for item in references)
        ):
            raise ValueError(f"{query_id} answer_references must be nonempty human strings")
        conditions = _require_mapping(case.get("conditions"), f"{query_id} conditions")
        if set(conditions) != set(CONDITIONS):
            raise ValueError(f"{query_id} must register actual, sufficient, and removed")
        for condition in CONDITIONS:
            _records(conditions, condition, owner=query_id)
        if query_id in seen:
            raise ValueError("dataset case identities must be unique")
        seen.add(query_id)
    return cases


def _dataset_controls(dataset: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    controls = dataset.get("natural_controls")
    if not isinstance(controls, list) or not controls:
        raise ValueError("dataset natural_controls must be a nonempty list")
    seen: set[str] = set()
    for control in controls:
        _require_mapping(control, "natural control")
        query_id = _identifier(control.get("query_id"), "control query_id")
        _identifier(control.get("paper_id"), f"{query_id} paper_id")
        conditions = _require_mapping(control.get("conditions"), f"{query_id} conditions")
        if set(conditions) != {"actual"}:
            raise ValueError("natural controls require only actual evidence")
        _records(conditions, "actual", owner=query_id)
        if query_id in seen:
            raise ValueError("natural control identities must be unique")
        if query_id in {
            _identifier(case.get("query_id"), "case query_id") for case in dataset["cases"]
        }:
            raise ValueError("natural controls and constructed cases must not mix")
        seen.add(query_id)
    return controls


def _row_strata(
    rows: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if not isinstance(rows, Mapping) or set(rows) != {"cases", "natural_controls"}:
        raise ValueError("rows must explicitly separate cases and natural_controls")
    case_rows = rows["cases"]
    control_rows = rows["natural_controls"]
    if not isinstance(case_rows, list) or not isinstance(control_rows, list):
        raise ValueError("row strata must be lists")
    if any(not isinstance(row, Mapping) for row in case_rows + control_rows):
        raise ValueError("rows must contain objects")
    return case_rows, control_rows


def _row_index(
    rows: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    *,
    stratum: str,
) -> dict[str, Mapping[str, Any]]:
    allowed = {
        case["query_id"]: set(CONDITIONS if stratum == "cases" else ("actual",)) for case in cases
    }
    indexed: dict[str, Mapping[str, Any]] = {}
    expected_position = {case["query_id"]: index for index, case in enumerate(cases)}
    previous_position = -1
    for row in rows:
        query_id = row.get("query_id")
        if type(query_id) is not str or query_id not in allowed:
            raise ValueError(f"{stratum} rows contain an unregistered query_id")
        if query_id in indexed:
            raise ValueError(f"{stratum} rows contain duplicate query_id values")
        position = expected_position[query_id]
        if position <= previous_position:
            raise ValueError(f"{stratum} rows must preserve registered order")
        previous_position = position
        indexed[query_id] = row
    for case in cases:
        indexed.setdefault(
            case["query_id"],
            {
                "query_id": case["query_id"],
                "paper_id": case["paper_id"],
                "conditions": {},
            },
        )
    return indexed


def _score_case(
    case: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    query_id = case["query_id"]
    rejected = row.get("paper_id") != case["paper_id"]
    conditions = row.get("conditions")
    if not isinstance(conditions, Mapping):
        conditions = {}
        rejected = True
    if set(conditions) != set(CONDITIONS):
        rejected = True
    scored: dict[str, dict[str, Any]] = {}
    for condition in CONDITIONS:
        result = conditions.get(condition) if row.get("paper_id") == case["paper_id"] else None
        if not isinstance(result, Mapping):
            result = _missing_result()
            rejected = True
        scored[condition] = _score_result(
            result,
            case,
            condition=condition,
            query_id=query_id,
        )
    actual = scored["actual"]
    sufficient = scored["sufficient"]
    removed = scored["removed"]
    positive_exact = bool(actual["valid"] and actual["exact_match"] == 1)
    negative_correct = removed["correct_refusal"]
    return {
        "query_id": query_id,
        "paper_id": case["paper_id"],
        "row_rejected": rejected,
        "conditions": scored,
        "positive_exact_and_negative_correct_refusal": positive_exact and negative_correct,
        "sufficient_exact_and_negative_correct_refusal": bool(
            sufficient["valid"] and sufficient["exact_match"] == 1 and negative_correct
        ),
    }


def _score_control(
    control: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    rejected = row.get("paper_id") != control["paper_id"]
    conditions = row.get("conditions")
    if (
        row.get("paper_id") != control["paper_id"]
        or not isinstance(conditions, Mapping)
        or set(conditions) != {"actual"}
    ):
        result = _missing_result()
        rejected = True
    else:
        result = conditions["actual"]
        if not isinstance(result, Mapping):
            result = _missing_result()
            rejected = True
    return {
        "query_id": control["query_id"],
        "paper_id": control["paper_id"],
        "row_rejected": rejected,
        "result": _score_result(
            result,
            control,
            condition="actual",
            query_id=control["query_id"],
        ),
    }


def _score_result(
    result: Mapping[str, Any],
    case: Mapping[str, Any],
    *,
    condition: str,
    query_id: str,
) -> dict[str, Any]:
    raw_status = result.get("status")
    status = raw_status if raw_status in RESULT_STATUSES else "INVALID_OUTPUT"
    structure_valid = _valid_result_structure(
        result,
        case,
        condition=condition,
        query_id=query_id,
    )
    valid = status == "VALID" and structure_valid
    answer = result.get("answer")
    valid_refusal = (
        valid
        and result.get("unanswerable") is True
        and answer == ""
        and result.get("cited_source_ids") == []
    )
    correct_refusal = valid_refusal
    unsupported = bool(
        valid and result.get("unanswerable") is False and isinstance(answer, str) and answer.strip()
    )
    if valid and condition in {"actual", "sufficient"} and "answer_references" in case:
        quality = answer_metrics(result["answer"], list(case["answer_references"]))
        exact_match, token_f1 = quality["exact_match"], quality["token_f1"]
    else:
        exact_match = token_f1 = 0.0
    return {
        "status": status,
        "valid": valid,
        "structure_valid": structure_valid,
        "exact_match": exact_match,
        "token_f1": token_f1,
        "valid_refusal": valid_refusal,
        "correct_refusal": correct_refusal,
        "unsupported_nonrefusal": unsupported,
        "usage": result.get("usage"),
        "latency_ms": result.get("latency_ms"),
    }


def _valid_result_structure(
    result: Mapping[str, Any],
    case: Mapping[str, Any],
    *,
    condition: str,
    query_id: str,
) -> bool:
    if not REQUIRED_RESULT_FIELDS.issubset(result):
        return False
    answer = result["answer"]
    refused = result["unanswerable"]
    cited = result["cited_source_ids"]
    usage = result["usage"]
    latency = result["latency_ms"]
    if type(answer) is not str or type(refused) is not bool or not isinstance(cited, list):
        return False
    if any(type(source_id) is not str or not source_id for source_id in cited):
        return False
    if len(cited) != len(set(cited)):
        return False
    visible = {record["source_id"] for record in case["conditions"][condition]["records"]}
    if any(source_id not in visible for source_id in cited):
        return False
    if refused and (answer != "" or cited):
        return False
    if not refused and (not answer.strip() or not cited):
        return False
    if usage is not None and not _valid_usage(usage):
        return False
    if latency is not None and (
        isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0
    ):
        return False
    return True


def _valid_usage(value: object) -> bool:
    if not isinstance(value, Mapping) or set(USAGE_FIELDS) - set(value):
        return False
    for field in USAGE_FIELDS:
        item = value[field]
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            return False
    input_tokens, output_tokens, total_tokens = (value[field] for field in USAGE_FIELDS)
    return (
        total_tokens is None
        or input_tokens is None
        or output_tokens is None
        or total_tokens == input_tokens + output_tokens
    )


def _identifier_metrics(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [result for result in results if result["valid"]]
    return {
        "valid": len(valid),
        "invalid_or_error": len(results) - len(valid),
        "validity_is_not_entailment": True,
        "status_counts": dict(sorted(Counter(result["status"] for result in results).items())),
        "structure_invalid_valid_status": sum(
            result["status"] == "VALID" and not result["structure_valid"] for result in results
        ),
    }


def _main_report(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    denominator = len(cases)
    condition_reports: dict[str, dict[str, Any]] = {}
    for condition in CONDITIONS:
        values = [case["conditions"][condition] for case in cases]
        valid_values = [value for value in values if value["valid"]]
        report: dict[str, Any] = {
            "planned": denominator,
            "valid": len(valid_values),
            "invalid_or_error": denominator - len(valid_values),
            "status_counts": dict(sorted(Counter(value["status"] for value in values).items())),
        }
        if condition in {"actual", "sufficient"}:
            report.update(
                {
                    "token_f1": fmean(value["token_f1"] for value in values) if values else 0.0,
                    "exact_match": fmean(value["exact_match"] for value in values)
                    if values
                    else 0.0,
                    "valid_refusals": sum(value["valid_refusal"] for value in valid_values),
                    "valid_refusal_rate": _rate(
                        sum(value["valid_refusal"] for value in valid_values),
                        denominator,
                    ),
                }
            )
        else:
            report.update(
                {
                    "correct_refusals": sum(value["correct_refusal"] for value in values),
                    "correct_refusal_rate": _rate(
                        sum(value["correct_refusal"] for value in values),
                        denominator,
                    ),
                    "unsupported_nonrefusals": sum(
                        value["unsupported_nonrefusal"] for value in values
                    ),
                    "unsupported_nonrefusal_rate": _rate(
                        sum(value["unsupported_nonrefusal"] for value in values),
                        denominator,
                    ),
                    "invalid_or_error_rate": _rate(denominator - len(valid_values), denominator),
                }
            )
        condition_reports[condition] = report
    positive_actual = sum(
        case["conditions"]["actual"]["valid"] and case["conditions"]["actual"]["exact_match"] == 1
        for case in cases
    )
    positive_sufficient = sum(
        case["conditions"]["sufficient"]["valid"]
        and case["conditions"]["sufficient"]["exact_match"] == 1
        for case in cases
    )
    paired_actual = sum(case["positive_exact_and_negative_correct_refusal"] for case in cases)
    paired_sufficient = sum(case["sufficient_exact_and_negative_correct_refusal"] for case in cases)
    return {
        "denominator": denominator,
        "conditions": condition_reports,
        "paired": {
            "denominator": denominator,
            "actual_exact_and_removed_correct_refusal": paired_actual,
            "actual_exact_and_removed_correct_refusal_rate": _rate(paired_actual, denominator),
            "sufficient_exact_and_removed_correct_refusal": paired_sufficient,
            "sufficient_exact_and_removed_correct_refusal_rate": _rate(
                paired_sufficient, denominator
            ),
            "positive_exact_actual": positive_actual,
            "positive_exact_sufficient": positive_sufficient,
            "removed_correct_refusals": condition_reports["removed"]["correct_refusals"],
            "positive_actual_exact_rate": _rate(positive_actual, denominator),
            "positive_sufficient_exact_rate": _rate(positive_sufficient, denominator),
            "removed_correct_refusal_rate": condition_reports["removed"]["correct_refusal_rate"],
        },
        "cases": [
            {
                "query_id": case["query_id"],
                "paper_id": case["paper_id"],
                "row_rejected": case["row_rejected"],
                "actual": case["conditions"]["actual"],
                "sufficient": case["conditions"]["sufficient"],
                "removed": case["conditions"]["removed"],
                "paired_actual_negative_success": case[
                    "positive_exact_and_negative_correct_refusal"
                ],
                "paired_sufficient_negative_success": case[
                    "sufficient_exact_and_negative_correct_refusal"
                ],
            }
            for case in cases
        ],
    }


def _control_report(controls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [control["result"] for control in controls]
    denominator = len(values)
    valid = [value for value in values if value["valid"]]
    correct_refusals = sum(value["correct_refusal"] for value in values)
    nonrefusals = sum(value["unsupported_nonrefusal"] for value in values)
    return {
        "denominator": denominator,
        "valid": len(valid),
        "valid_correct_refusals": correct_refusals,
        "valid_correct_refusal_rate": _rate(correct_refusals, denominator),
        "valid_nonrefusals": nonrefusals,
        "valid_nonrefusal_rate": _rate(nonrefusals, denominator),
        "invalid_or_error": denominator - len(valid),
        "invalid_or_error_rate": _rate(denominator - len(valid), denominator),
        "status_counts": dict(sorted(Counter(value["status"] for value in values).items())),
        "cases": [
            {
                "query_id": control["query_id"],
                "paper_id": control["paper_id"],
                "row_rejected": control["row_rejected"],
                **control["result"],
            }
            for control in controls
        ],
        "never_combined_with_constructed_negatives_or_answerable_f1": True,
    }


def _paper_cluster_diagnostics(
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    groups = {case["query_id"]: case["paper_id"] for case in cases}
    paper_count = len(set(groups.values()))
    complete = all(
        case["conditions"][condition]["valid"]
        for case in cases
        for condition in ("actual", "sufficient")
    )
    if not complete:
        status = "INCOMPLETE_INPUTS"
    elif paper_count < 2:
        status = "INSUFFICIENT_PAPER_CLUSTERS"
    else:
        status = "DESCRIPTIVE_ONLY"

    def interval(metric: str, left: str, right: str) -> dict[str, Any]:
        baseline = {case["query_id"]: float(case["conditions"][left][metric]) for case in cases}
        candidate = {case["query_id"]: float(case["conditions"][right][metric]) for case in cases}
        if status != "DESCRIPTIVE_ONLY":
            return {
                "status": status,
                "estimate": fmean(candidate[query_id] - baseline[query_id] for query_id in baseline)
                if baseline
                else None,
                "ci95_low": None,
                "ci95_high": None,
                "papers": paper_count,
            }
        result = grouped_difference(baseline, candidate, groups)
        return {
            "status": status,
            "estimate": result["estimate_ms"],
            "ci95_low": result["ci95_low_ms"],
            "ci95_high": result["ci95_high_ms"],
            "queries": result["queries"],
            "papers": result["groups"],
            "bootstrap_samples": result["bootstrap_samples"],
            "direction": "right minus left; descriptive only",
        }

    return {
        "status": status,
        "paper_groups": paper_count,
        "sufficient_minus_actual_token_f1": interval("token_f1", "actual", "sufficient"),
        "sufficient_minus_actual_exact_match": interval("exact_match", "actual", "sufficient"),
        "no_p_values": True,
        "not_method_promotion": True,
    }


def _usage_report(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    usage_values = [
        result["usage"] if _valid_usage(result["usage"]) else None for result in results
    ]
    totals: dict[str, int | None] = {}
    known_totals: dict[str, int] = {}
    unknown_counts: dict[str, int] = {}
    for field in USAGE_FIELDS:
        values = [
            value[field] for value in usage_values if value is not None and value[field] is not None
        ]
        known = sum(values)
        known_totals[field] = known
        unknown = len(results) - len(values)
        totals[field] = None if unknown else known
        unknown_counts[field] = unknown
    latency = [
        float(result["latency_ms"])
        for result in results
        if result["latency_ms"] is not None
        and not isinstance(result["latency_ms"], bool)
        and isinstance(result["latency_ms"], (int, float))
    ]
    return {
        "totals": totals,
        "known_totals": known_totals,
        "unknown_counts": unknown_counts,
        "known_counts": {field: len(results) - unknown_counts[field] for field in USAGE_FIELDS},
        "latency_ms": {
            "n": len(latency),
            "p50": quantile(latency, 0.5),
            "p95": quantile(latency, 0.95),
        },
        "cost": None,
    }


def _ledger_audit(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    status_values = [
        {
            str(entry[field]).upper()
            for field in ("annotation_state", "review_status", "status")
            if entry.get(field) is not None
        }
        for entry in entries
    ]
    proposed = sum(any("PROPOSED" in status for status in statuses) for statuses in status_values)
    pending = sum(
        any("PENDING" in status or "UNADJUDICATED" in status for status in statuses)
        for statuses in status_values
    )
    ineligible = sum(entry.get("selected") is False for entry in entries)
    active = sum(
        entry.get("selected") is True or any("REVIEWED" in status for status in statuses)
        for entry, statuses in zip(entries, status_values, strict=True)
    )
    return {
        "proposed_ledger_cases": proposed,
        "pending_ledger_cases": pending,
        "ineligible_ledger_cases": ineligible,
        "active_ledger_cases": active,
    }


def _records(
    conditions: Mapping[str, Any],
    condition: str,
    *,
    owner: str,
) -> list[Mapping[str, Any]]:
    value = conditions.get(condition)
    _require_mapping(value, f"{owner}/{condition}")
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError(f"{owner}/{condition} records must be a list")
    seen: set[str] = set()
    for record in records:
        _require_mapping(record, f"{owner}/{condition} record")
        source_id = _identifier(record.get("source_id"), f"{owner}/{condition} source_id")
        _nonempty_string(record.get("text"), f"{owner}/{condition} text")
        if source_id in seen:
            raise ValueError(f"{owner}/{condition} contains duplicate source_id values")
        seen.add(source_id)
    return records


def _missing_result() -> dict[str, Any]:
    return {
        "status": "NOT_ATTEMPTED",
        "answer": None,
        "unanswerable": None,
        "cited_source_ids": [],
        "usage": None,
        "latency_ms": None,
    }


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _nonempty_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


__all__ = ["score_diagnostic"]
