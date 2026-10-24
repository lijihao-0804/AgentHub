"""Offline scoring and gates for the fixed original/supported prompt trial."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from math import isfinite
from statistics import fmean

from benchmarks.public_rag.attributed_reader_runner import from_wire
from benchmarks.public_rag.consumer_answer_metrics import answer_metrics
from benchmarks.public_rag.experimental.attributed_reader import (
    validate_reader_answer,
    visible_records,
)
from benchmarks.public_rag.scenario_metrics import _percentile, paired_group_ci
from packages.model_gateway.contracts import ModelRequest

ARMS = ("baseline", "task_contract")
COHORTS = ("development", "source_contrast")
CONDITIONS = ("original", "supported", "deleted")
PRIMARY = ("development", "original")
ACTIVE_STATUSES = frozenset(
    {
        "VALID",
        "INVALID_OUTPUT",
        "PROVIDER_ERROR",
        "UNKNOWN_OUTCOME",
        "MODEL_IDENTITY_ERROR",
    }
)
FAILURE_STATUSES = frozenset(
    {"INVALID_OUTPUT", "PROVIDER_ERROR", "UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"}
)
GATE_KEYS = frozenset(
    {
        "minimum_f1_gain",
        "minimum_ci_low",
        "maximum_em_loss",
        "maximum_input_ratio",
        "maximum_p95_ratio",
        "minimum_valid_fraction",
        "bootstrap_samples",
        "confidence",
        "maximum_deleted_answer_increase",
        "maximum_supported_refusal_increase",
    }
)


def summarize(dataset: dict, rows: list[dict], protocol: dict, complete: bool) -> dict:
    """Score all planned cases, including failures and unattempted rows.

    Answer quality and paired inference are restricted to development/original.
    Deleted cases only evaluate refusal; supported source IDs are used solely to
    diagnose citation IDs and are not a semantic-faithfulness judgment.
    """

    if type(complete) is not bool:
        raise TypeError("complete must be a boolean")
    gates_config = _validate(dataset, rows, protocol)
    cases = dataset["cases"]
    rows_by_id = {row["query_id"]: row for row in rows}
    scored = [_score_case(case, rows_by_id[case["query_id"]]) for case in cases]
    summaries = {
        cohort: {
            condition: _summarize_slice(
                [item for item in scored if _slice(item) == (cohort, condition)]
            )
            for condition in CONDITIONS
            if any(_slice(item) == (cohort, condition) for item in scored)
        }
        for cohort in COHORTS
    }
    development = [item for item in scored if item["case"]["cohort"] == "development"]
    primary_rows = [item for item in development if item["case"]["condition"] == "original"]
    contrast = [item for item in scored if item["case"]["cohort"] == "source_contrast"]
    comparison, comparison_rows = _comparison(primary_rows, protocol)
    cost = _cost(primary_rows)
    physical = _physical_api(rows)
    deletion_contrast, deletion_evaluation = _pair_contrast(contrast, "deleted")
    supported_contrast, supported_evaluation = _pair_contrast(contrast, "supported")
    citation = _citation_diagnostics(scored)
    gates = {
        "complete": bool(complete),
        "f1_minimum_gain": comparison["delta"] >= gates_config["minimum_f1_gain"],
        "f1_group_ci_low": (
            comparison["inference_valid"] and comparison["ci_low"] > gates_config["minimum_ci_low"]
        ),
        "em_loss": comparison["em_delta"] >= -gates_config["maximum_em_loss"],
        "usage_complete": cost["complete_coverage"],
        "input_ratio": (
            cost["complete_coverage"]
            and cost["mean_input_ratio"] is not None
            and cost["mean_input_ratio"] <= gates_config["maximum_input_ratio"]
        ),
        "p95_ratio": (
            cost["complete_latency_coverage"]
            and cost["p95_ratio"] is not None
            and cost["p95_ratio"] <= gates_config["maximum_p95_ratio"]
        ),
        "valid_fraction": (
            min(summaries[PRIMARY[0]][PRIMARY[1]]["arms"][arm]["valid_fraction"] for arm in ARMS)
            >= gates_config["minimum_valid_fraction"]
        ),
        "contrast_valid_fraction": (
            bool(contrast)
            and all(
                summaries["source_contrast"][condition]["arms"][arm]["valid_fraction"]
                >= gates_config["minimum_valid_fraction"]
                for condition in ("supported", "deleted")
                for arm in ARMS
            )
        ),
        "known_outcomes_and_model": (
            physical["statuses"].get("UNKNOWN_OUTCOME", 0) == 0
            and physical["statuses"].get("MODEL_IDENTITY_ERROR", 0) == 0
        ),
        "deleted_answer_nonincrease": (
            deletion_contrast["answer_rate_delta"] is not None
            and deletion_contrast["answer_rate_delta"]
            <= gates_config["maximum_deleted_answer_increase"]
        ),
        "supported_refusal_nonincrease": (
            supported_contrast["refusal_rate_delta"] is not None
            and supported_contrast["refusal_rate_delta"]
            <= gates_config["maximum_supported_refusal_increase"]
        ),
        "paired_groups": len({item["case"]["group_id"] for item in primary_rows}) >= 2,
        "planned_population": all(
            item["planned"] for item in scored if item["case"]["cohort"] == "development"
        ),
    }
    return {
        "scope": (
            "Fixed prompt trial over all planned cases; answer metrics use only "
            "development/original and failures remain in the denominator."
        ),
        "arms": summaries,
        "comparison": comparison,
        "comparison_rows": comparison_rows,
        "cost": cost,
        "physical_api": physical,
        "source_contrast": {
            "deleted": deletion_contrast,
            "supported": supported_contrast,
            "case_evaluation": {
                "deleted": deletion_evaluation,
                "supported": supported_evaluation,
            },
        },
        "citation_diagnostics": citation,
        "gates": gates,
        "all_passed": all(gates.values()),
        "default_changed": False,
        "semantic_judge_used": False,
        "protocol_seed": protocol["seed"],
        "denominators": _denominators(development, contrast),
        "limitations": [
            "Failures and NOT_ATTEMPTED rows score zero and retain their planned denominator.",
            "Deleted success requires VALID, unanswerable=true, empty answer, and no citations.",
            "Supported source IDs diagnose citation identity only, never faithfulness.",
            "The main comparison excludes source_contrast and all deleted cases.",
        ],
    }


def _validate(dataset: dict, rows: list[dict], protocol: dict) -> dict:
    if type(dataset) is not dict or type(dataset.get("cases")) is not list:
        raise ValueError("dataset must contain a cases list")
    if type(rows) is not list:
        raise ValueError("rows must be a list")
    if type(protocol) is not dict or not {"gates", "seed"} <= set(protocol):
        raise ValueError("protocol must contain gates and seed")
    config = protocol["gates"]
    if type(config) is not dict or set(config) != GATE_KEYS:
        raise ValueError("protocol gates are incomplete or unknown")
    if type(protocol["seed"]) is not int or isinstance(protocol["seed"], bool):
        raise ValueError("protocol seed must be an integer")
    for key in (
        "minimum_f1_gain",
        "minimum_ci_low",
        "maximum_em_loss",
        "maximum_input_ratio",
        "maximum_p95_ratio",
        "minimum_valid_fraction",
        "maximum_deleted_answer_increase",
        "maximum_supported_refusal_increase",
    ):
        value = config[key]
        if type(value) not in {int, float} or isinstance(value, bool) or not isfinite(value):
            raise ValueError(f"{key} must be finite")
    if (
        type(config["bootstrap_samples"]) is not int
        or isinstance(config["bootstrap_samples"], bool)
        or config["bootstrap_samples"] < 1
    ):
        raise ValueError("bootstrap_samples must be a positive integer")
    if not 0 < config["confidence"] < 1:
        raise ValueError("confidence must be between 0 and 1")
    if not 0 <= config["minimum_valid_fraction"] <= 1:
        raise ValueError("minimum_valid_fraction must be between 0 and 1")

    cases = dataset["cases"]
    if not cases:
        raise ValueError("dataset must contain planned cases")
    case_ids = [_required_id(case.get("query_id"), "case query_id") for case in cases]
    row_ids = [_required_id(row.get("query_id"), "row query_id") for row in rows]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate case query_id")
    if len(set(row_ids)) != len(row_ids):
        raise ValueError("duplicate row query_id")
    if set(case_ids) != set(row_ids):
        missing = sorted(set(case_ids) - set(row_ids))
        extra = sorted(set(row_ids) - set(case_ids))
        raise ValueError(f"row query_id population differs: missing={missing}, extra={extra}")

    for case in cases:
        query_id = case["query_id"]
        _required_id(case.get("group_id"), "group_id")
        if case.get("cohort") not in COHORTS:
            raise ValueError(f"invalid cohort for {query_id}")
        if case.get("condition") not in CONDITIONS:
            raise ValueError(f"invalid condition for {query_id}")
        if (case["cohort"] == "development" and case["condition"] != "original") or (
            case["cohort"] == "source_contrast" and case["condition"] == "original"
        ):
            raise ValueError(f"invalid cohort/condition combination for {query_id}")
        aliases = case.get("aliases")
        if (
            type(aliases) is not list
            or not aliases
            or any(type(alias) is not str or not alias.strip() for alias in aliases)
        ):
            raise ValueError(f"invalid aliases for {query_id}")
        messages = case.get("messages")
        if type(messages) is not list:
            raise ValueError(f"messages must be a list for {query_id}")
        request = _request(case)
        try:
            _, _, records = visible_records(request)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid visible sources for {query_id}") from exc
        pair_id = case.get("pair_id")
        if pair_id is not None:
            _required_id(pair_id, "pair_id")
        source_ids = case.get("supported_source_ids")
        if source_ids is not None:
            _required_ids(source_ids, "supported_source_ids")
            if not set(source_ids) <= {record["chunk_id"] for record in records}:
                raise ValueError(f"supported_source_ids must be visible sources for {query_id}")

    for row in rows:
        query_id = row["query_id"]
        arms = row.get("arms")
        if type(arms) is not dict or set(arms) != set(ARMS):
            raise ValueError(f"row arms must be exactly baseline and task_contract for {query_id}")
        if any(type(arms[arm]) is not dict for arm in ARMS):
            raise ValueError(f"row arm must be an object for {query_id}")
        for arm in ARMS:
            _validate_api(arms[arm].get("api"), query_id)

    _validate_pairs(cases)
    _validate_physical_ids(rows)
    return config


def _validate_pairs(cases: list[dict]) -> None:
    pairs: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        pair_id = case.get("pair_id")
        if pair_id is not None:
            pairs[pair_id].append(case)
    for case in cases:
        if case["cohort"] == "source_contrast" and case.get("pair_id") is None:
            raise ValueError(f"source contrast pair_id required for {case['query_id']}")
    for pair_id, members in pairs.items():
        conditions = [case["condition"] for case in members]
        if sorted(conditions) != ["deleted", "supported"]:
            raise ValueError(f"source contrast pair {pair_id!r} must be exactly supported+deleted")
        if len({case["group_id"] for case in members}) != 1:
            raise ValueError(f"source contrast pair {pair_id!r} group_id differs")
        if len({tuple(case["aliases"]) for case in members}) != 1:
            raise ValueError(f"source contrast pair {pair_id!r} aliases differ")


def _validate_api(api: object, query_id: str) -> None:
    if type(api) is not dict:
        raise ValueError(f"api must be an object for {query_id}")
    status = api.get("status")
    if type(status) is not str:
        raise ValueError(f"api status missing for {query_id}")
    physical_id = api.get("physical_id")
    if physical_id is not None:
        _required_id(physical_id, "physical_id")
    if status == "NOT_ATTEMPTED":
        if physical_id is not None:
            raise ValueError(f"NOT_ATTEMPTED cannot have physical_id for {query_id}")
        return
    if status not in ACTIVE_STATUSES:
        raise ValueError(f"unknown api status {status!r} for {query_id}")
    if physical_id is None:
        raise ValueError(f"attempted api requires physical_id for {query_id}")
    usage, request_ms = api.get("usage"), api.get("request_ms")
    if usage is not None:
        _validate_usage(usage, query_id)
    if request_ms is not None and (
        type(request_ms) not in {int, float}
        or isinstance(request_ms, bool)
        or not isfinite(request_ms)
        or request_ms < 0
    ):
        raise ValueError(f"invalid request_ms for {query_id}")
    if status == "VALID":
        for key in ("answer", "unanswerable", "cited_chunk_ids"):
            if key not in api:
                raise ValueError(f"VALID api missing {key} for {query_id}")
        if api.get("reused_from") is not None:
            _required_id(api["reused_from"], "reused_from")


def _validate_usage(usage: object, query_id: str) -> None:
    if type(usage) is not dict:
        raise ValueError(f"usage must be an object for {query_id}")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid {key} for {query_id}")


def _validate_physical_ids(rows: list[dict]) -> None:
    seen: dict[str, dict] = {}
    owners: dict[str, tuple[str, str]] = {}
    rows_by_id = {row["query_id"]: row for row in rows}
    for row in rows:
        for arm in ARMS:
            api = row["arms"][arm]["api"]
            physical_id = api.get("physical_id")
            if physical_id is None:
                continue
            comparable = {k: v for k, v in api.items() if k != "reused_from"}
            prior = seen.get(physical_id)
            if prior is not None and prior != comparable:
                raise ValueError(f"conflicting api payload for physical_id {physical_id!r}")
            seen[physical_id] = comparable
            owners.setdefault(physical_id, (row["query_id"], arm))
            reused_from = api.get("reused_from")
            if reused_from is not None:
                owner = rows_by_id.get(reused_from)
                if owner is None:
                    raise ValueError(f"reused_from {reused_from!r} is not a planned query")
                owner_query_id, owner_arm = owners[physical_id]
                owner_api = rows_by_id[owner_query_id]["arms"][owner_arm]["api"]
                if (
                    owner_query_id != reused_from
                    or owner_arm != arm
                    or owner_api.get("physical_id") != physical_id
                ):
                    raise ValueError(
                        f"reused api for {row['query_id']} must retain its original physical_id"
                    )


def _score_case(case: dict, row: dict) -> dict:
    result = {"case": case, "arms": {}, "planned": True}
    for arm in ARMS:
        api = row["arms"][arm]["api"]
        valid = api["status"] == "VALID"
        validation_error = None
        if valid:
            try:
                parsed = validate_reader_answer(
                    _answer_json(api),
                    _visible_records(case),
                )
                api = {**api, **parsed}
            except (TypeError, ValueError) as exc:
                valid = False
                validation_error = str(exc)
        answer = api.get("answer", "") if valid else ""
        quality = answer_metrics(answer, case["aliases"])
        refused = bool(
            valid and api["unanswerable"] and api["answer"] == "" and api["cited_chunk_ids"] == []
        )
        answer_attempt = bool(valid and not api["unanswerable"] and bool(api["answer"].strip()))
        result["arms"][arm] = {
            **quality,
            "valid": valid,
            "status": (
                "INVALID_OUTPUT"
                if api["status"] == "VALID" and validation_error is not None
                else api["status"]
            ),
            "refused": refused,
            "failure": (
                api["status"] in FAILURE_STATUSES or (api["status"] == "VALID" and not valid)
            ),
            "validation_error": validation_error,
            "api": api,
            "prompt_tokens": _token(api, "prompt_tokens"),
            "completion_tokens": _token(api, "completion_tokens"),
            "total_tokens": _token(api, "total_tokens"),
            "request_ms": (
                api.get("request_ms")
                if type(api.get("request_ms")) in {int, float}
                and not isinstance(api.get("request_ms"), bool)
                and isfinite(api.get("request_ms"))
                and api.get("request_ms") >= 0
                else None
            ),
            "unsupported_answer": (
                case["condition"] == "deleted"
                and api["status"] != "NOT_ATTEMPTED"
                and valid
                and answer_attempt
            ),
            "supported_false_refusal": (
                case["condition"] == "supported" and api["status"] != "NOT_ATTEMPTED" and refused
            ),
            "correct_refusal": (
                case["condition"] == "deleted" and api["status"] != "NOT_ATTEMPTED" and refused
            ),
        }
        result["arms"][arm]["usage_complete"] = all(
            result["arms"][arm][key] is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
    return result


def _summarize_slice(items: list[dict]) -> dict:
    if not items:
        raise ValueError("cannot summarize an empty cohort/condition")
    return {
        "queries": len(items),
        "groups": len({item["case"]["group_id"] for item in items}),
        "arms": {arm: _arm_summary(items, arm) for arm in ARMS},
    }


def _arm_summary(items: list[dict], arm: str) -> dict:
    values = [item["arms"][arm] for item in items]
    n = len(values)
    usage_complete = sum(value["usage_complete"] for value in values)
    return {
        "planned": n,
        "valid": sum(value["valid"] for value in values),
        "valid_fraction": sum(value["valid"] for value in values) / n,
        "failures": sum(value["failure"] for value in values),
        "not_attempted": sum(value["status"] == "NOT_ATTEMPTED" for value in values),
        "statuses": dict(Counter(value["status"] for value in values)),
        "invalid_validation": sum(value["validation_error"] is not None for value in values),
        "token_f1": fmean(value["token_f1"] for value in values),
        "exact_match": fmean(value["exact_match"] for value in values),
        "refusal_rate": sum(value["refused"] for value in values) / n,
        "correct_refusal_rate": sum(value["correct_refusal"] for value in values) / n,
        "unsupported_answer_rate": sum(value["unsupported_answer"] for value in values) / n,
        "supported_false_refusal_rate": (
            sum(value["supported_false_refusal"] for value in values) / n
        ),
        "usage_observed": usage_complete,
        "usage_unknown": n - usage_complete,
        "attempted": sum(value["status"] != "NOT_ATTEMPTED" for value in values),
    }


def _comparison(items: list[dict], protocol: dict) -> tuple[dict, list[dict]]:
    if not items:
        raise ValueError("development/original planned cases required")
    comparison_rows = [
        {
            "query_id": item["case"]["query_id"],
            "group_id": item["case"]["group_id"],
            "arms": {
                arm: {
                    "token_f1": item["arms"][arm]["token_f1"],
                    "exact_match": item["arms"][arm]["exact_match"],
                    "valid": item["arms"][arm]["valid"],
                }
                for arm in ARMS
            },
        }
        for item in items
    ]
    config = protocol["gates"]
    f1 = paired_group_ci(
        comparison_rows,
        "token_f1",
        "baseline",
        "task_contract",
        group_key="group_id",
        seed=protocol["seed"],
        samples=config["bootstrap_samples"],
        confidence=config["confidence"],
    )
    em = paired_group_ci(
        comparison_rows,
        "exact_match",
        "baseline",
        "task_contract",
        group_key="group_id",
        seed=protocol["seed"],
        samples=config["bootstrap_samples"],
        confidence=config["confidence"],
    )
    result = {
        **f1,
        "primary_cohort": PRIMARY[0],
        "primary_condition": PRIMARY[1],
        "planned_queries": len(items),
        "em_baseline_mean": em["baseline_mean"],
        "em_candidate_mean": em["candidate_mean"],
        "em_delta": em["delta"],
        "em_ci_low": em["ci_low"],
        "em_ci_high": em["ci_high"],
        "answer_metrics_denominator": len(items),
    }
    return result, comparison_rows


def _cost(items: list[dict]) -> dict:
    input_pairs, latency_pairs = [], []
    missing_usage = 0
    missing_latency = 0
    for item in items:
        baseline, candidate = (item["arms"][arm] for arm in ARMS)
        if not baseline["usage_complete"] or not candidate["usage_complete"]:
            missing_usage += 1
        else:
            input_pairs.append((baseline["prompt_tokens"], candidate["prompt_tokens"]))
        if baseline["request_ms"] is None or candidate["request_ms"] is None:
            missing_latency += 1
        else:
            latency_pairs.append((baseline["request_ms"], candidate["request_ms"]))

    mean_input = (
        fmean(candidate for _, candidate in input_pairs)
        / fmean(baseline for baseline, _ in input_pairs)
        if input_pairs and fmean(baseline for baseline, _ in input_pairs) > 0
        else None
    )
    baseline_p95 = (
        _percentile([baseline for baseline, _ in latency_pairs], 0.95) if latency_pairs else None
    )
    candidate_p95 = (
        _percentile([candidate for _, candidate in latency_pairs], 0.95) if latency_pairs else None
    )
    return {
        "planned_queries": len(items),
        "planned_arm_pairs": len(items) * len(ARMS),
        "usage_complete_pairs": len(input_pairs),
        "usage_missing_pairs": missing_usage,
        "complete_coverage": missing_usage == 0,
        "mean_input_ratio": mean_input,
        "latency_complete_pairs": len(latency_pairs),
        "latency_missing_pairs": missing_latency,
        "complete_latency_coverage": missing_latency == 0,
        "baseline_p95_ms": baseline_p95,
        "task_contract_p95_ms": candidate_p95,
        "p95_ratio": (
            candidate_p95 / baseline_p95
            if baseline_p95 is not None and baseline_p95 > 0 and candidate_p95 is not None
            else None
        ),
    }


def _physical_api(rows: list[dict]) -> dict:
    calls: dict[str, dict] = {}
    references = 0
    for row in rows:
        for arm in ARMS:
            api = row["arms"][arm]["api"]
            physical_id = api.get("physical_id")
            if api["status"] == "NOT_ATTEMPTED":
                continue
            references += 1
            if physical_id in calls:
                continue
            calls[physical_id] = api
    values = list(calls.values())
    statuses = Counter(call["status"] for call in values)
    total_tokens = [value for call in values if (value := _token(call, "total_tokens")) is not None]
    return {
        "unique_dispatches": len(values),
        "arm_row_references": references,
        "reused_row_references": references - len(values),
        "statuses": dict(statuses),
        "usage_observed": len(total_tokens),
        "usage_unknown": len(values) - len(total_tokens),
        "observed_total_tokens": sum(total_tokens),
        "currency_cost": None,
        "not_attempted_dispatches": 0,
    }


def _pair_contrast(items: list[dict], condition: str) -> tuple[dict, list[dict]]:
    selected = [item for item in items if item["case"]["condition"] == condition]
    paired_ids = set()
    for case in (item["case"] for item in items):
        pair = case.get("pair_id")
        if pair is not None:
            paired_ids.add(pair)
    complete_pairs = len(paired_ids)
    evaluation = [
        {
            "query_id": item["case"]["query_id"],
            "pair_id": item["case"].get("pair_id"),
            "arms": {
                arm: {
                    "valid": item["arms"][arm]["valid"],
                    "status": item["arms"][arm]["status"],
                    "correct_refusal": item["arms"][arm]["correct_refusal"],
                    "unsupported_answer": item["arms"][arm]["unsupported_answer"],
                    "supported_false_refusal": item["arms"][arm]["supported_false_refusal"],
                }
                for arm in ARMS
            },
        }
        for item in selected
    ]
    if condition == "deleted":
        rates = {arm: _rate(selected, arm, "unsupported_answer") for arm in ARMS}
        delta = (
            _delta(rates)
            if rates["baseline"] is not None and rates["task_contract"] is not None
            else None
        )
        return {
            "planned": len(selected),
            "pairs": complete_pairs,
            "unsupported_answer_rate": rates["baseline"],
            "task_contract_unsupported_answer_rate": rates["task_contract"],
            "answer_rate_delta": delta,
            "both_correct": _both_correct(items),
        }, evaluation
    rates = {arm: _rate(selected, arm, "supported_false_refusal") for arm in ARMS}
    delta = (
        _delta(rates)
        if rates["baseline"] is not None and rates["task_contract"] is not None
        else None
    )
    return {
        "planned": len(selected),
        "pairs": complete_pairs,
        "false_refusal_rate": rates["baseline"],
        "task_contract_false_refusal_rate": rates["task_contract"],
        "refusal_rate_delta": delta,
        "both_correct": _both_correct(items),
    }, evaluation


def _both_correct(items: list[dict]) -> dict:
    by_pair = defaultdict(dict)
    for item in items:
        pair_id = item["case"].get("pair_id")
        if pair_id is None:
            continue
        by_pair[pair_id][item["case"]["condition"]] = item

    correct = {arm: 0 for arm in ARMS}
    other_condition_correct = {arm: True for arm in ARMS}
    for pair in by_pair.values():
        supported, deleted = pair.get("supported"), pair.get("deleted")
        if supported is None or deleted is None:
            for arm in ARMS:
                other_condition_correct[arm] = False
            continue
        for arm in ARMS:
            if (
                supported["arms"][arm]["exact_match"] == 1
                and not supported["arms"][arm]["refused"]
                and deleted["arms"][arm]["correct_refusal"]
            ):
                correct[arm] += 1
            if not (
                supported["arms"][arm]["exact_match"] == 1 and not supported["arms"][arm]["refused"]
            ):
                other_condition_correct[arm] = False
    return {
        "pairs": len(by_pair),
        "baseline": correct["baseline"],
        "task_contract": correct["task_contract"],
        "other_condition_correct": other_condition_correct,
    }


def _citation_diagnostics(items: list[dict]) -> dict:
    eligible = [item for item in items if item["case"].get("supported_source_ids")]
    output = {}
    for arm in ARMS:
        unknown = total = missing = 0
        for item in eligible:
            value = item["arms"][arm]
            if not value["valid"]:
                missing += 1
                continue
            cited = set(value["api"]["cited_chunk_ids"])
            expected = set(item["case"]["supported_source_ids"])
            total += len(cited)
            unknown += len(cited - expected)
        output[arm] = {
            "eligible_queries": len(eligible),
            "citation_id_missing_queries": missing,
            "cited_ids": total,
            "outside_supported_source_ids": unknown,
        }
    return {
        "interpretation": "Citation ID membership only; this is not semantic faithfulness.",
        "arms": output,
    }


def _denominators(development: list[dict], contrast: list[dict]) -> dict:
    def counts(items: list[dict]) -> dict:
        return {
            condition: len([item for item in items if item["case"]["condition"] == condition])
            for condition in CONDITIONS
        }

    return {
        "development": counts(development),
        "source_contrast": counts(contrast),
        "all_planned": len(development) + len(contrast),
        "all_planned_arm_calls": (len(development) + len(contrast)) * len(ARMS),
    }


def _request(case: dict) -> ModelRequest:
    try:
        return ModelRequest(messages=from_wire(case["messages"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid wire messages") from exc


def _visible_records(case: dict) -> list[dict]:
    _, _, records = visible_records(_request(case))
    return records


def _answer_json(api: dict) -> str:
    return json.dumps(
        {
            "answer": api["answer"],
            "unanswerable": api["unanswerable"],
            "cited_chunk_ids": api["cited_chunk_ids"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _token(api: dict, key: str) -> int | None:
    usage = api.get("usage")
    if type(usage) is not dict:
        return None
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def _required_id(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _required_ids(value: object, field: str) -> list[str]:
    if (
        type(value) is not list
        or any(type(item) is not str or not item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{field} must be a list of unique non-empty strings")
    return value


def _slice(item: dict) -> tuple[str, str]:
    return item["case"]["cohort"], item["case"]["condition"]


def _rate(items: list[dict], arm: str, key: str) -> float | None:
    if not items:
        return None
    return sum(item["arms"][arm][key] for item in items) / len(items)


def _delta(rates: dict[str, float | None]) -> float:
    return rates["task_contract"] - rates["baseline"]


__all__ = ["summarize"]
