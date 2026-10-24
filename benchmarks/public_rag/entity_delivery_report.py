"""Pure scoring for the frozen entity-delivery development slice.

The evaluator consumes normalized results. It performs no model, network,
filesystem, or semantic-entailment work.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from random import Random
from statistics import fmean
from typing import Any
from unicodedata import normalize as unicode_normalize

from benchmarks.public_rag.consumer_answer_metrics import answer_metrics

CONDITIONS = ("full", "removed")
ARMS = ("baseline", "typed")
RESULT_STATUSES = frozenset(
    {
        "VALID",
        "INVALID_OUTPUT",
        "PROVIDER_ERROR",
        "UNKNOWN_OUTCOME",
        "NOT_ATTEMPTED",
    }
)
DELIVERY_STATUSES = frozenset({"ANSWERED", "PARTIAL", "INSUFFICIENT", "NEEDS_CLARIFICATION"})
RESULT_FIELDS = frozenset(
    {
        "status",
        "delivery_status",
        "answer_values",
        "explanation",
        "cited_source_ids",
        "usage",
        "latency_ms",
    }
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
MIN_CLUSTER_GROUPS = 8
BOOTSTRAP_SAMPLES = 5000
BOOTSTRAP_SEED = 20261024


def score_delivery(dataset: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Score all planned cases and retain per-case evidence for audit.

    A missing row, condition, or arm is represented as ``NOT_ATTEMPTED`` and
    receives zero credit. Unknown or duplicate row identities are rejected.
    Rates use the planned case count as their denominator; the number of
    supplied results is reported separately as ``observed``.
    """

    cases = _validated_cases(dataset)
    ledger = dataset.get("ledger")
    if not isinstance(ledger, list) or any(not isinstance(item, Mapping) for item in ledger):
        raise ValueError("dataset ledger must be a list of proposal objects")

    rows_by_query = _row_index(rows, cases)
    scored_cases = [_score_case(case, rows_by_query.get(case["query_id"])) for case in cases]
    aggregate = _aggregate(cases, scored_cases, ledger_size=len(ledger))
    return {
        "scope": (
            "Frozen entity-delivery development scoring. Alias and delivery-contract "
            "checks only; no semantic relation or completeness verification."
        ),
        "state": "FROZEN_DEVELOPMENT",
        "cases": scored_cases,
        "aggregate": aggregate,
    }


def _validated_cases(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(dataset, Mapping) or dataset.get("state") != "FROZEN_DEVELOPMENT":
        raise ValueError("dataset state must be FROZEN_DEVELOPMENT")
    raw_cases = dataset.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("dataset cases must be a nonempty list")

    cases: list[dict[str, Any]] = []
    query_ids: set[str] = set()
    paper_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise ValueError("each dataset case must be an object")
        query_id = _required_string(raw_case.get("query_id"), "case query_id")
        paper_id = _required_string(raw_case.get("paper_id"), "case paper_id")
        if query_id in query_ids:
            raise ValueError("dataset query_id values must be unique")
        if paper_id in paper_ids:
            raise ValueError("dataset paper_id values must be unique")
        query_ids.add(query_id)
        paper_ids.add(paper_id)

        references = raw_case.get("answer_references")
        if (
            not isinstance(references, list)
            or not references
            or any(type(reference) is not str or not reference.strip() for reference in references)
        ):
            raise ValueError(f"{query_id} answer_references must be nonempty strings")

        gold_entities = _gold_entities(raw_case.get("gold_entities"), query_id)
        gold_values = {entity["value"] for entity in gold_entities}
        conditions = _conditions(
            raw_case.get("conditions"),
            query_id=query_id,
            gold_values=gold_values,
        )
        cases.append(
            {
                "query_id": query_id,
                "paper_id": paper_id,
                "answer_references": list(references),
                "gold_entities": gold_entities,
                "gold_values": gold_values,
                "conditions": conditions,
            }
        )
    return cases


def _gold_entities(value: object, query_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{query_id} gold_entities must be a nonempty list")

    entities: list[dict[str, Any]] = []
    values: set[str] = set()
    occupied_aliases: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{query_id} gold entity must be an object")
        raw_value = _required_string(item.get("value"), f"{query_id} gold entity value")
        canonical_value = _canonical(raw_value)
        if canonical_value in values:
            raise ValueError(f"{query_id} gold entity values must be unique after normalization")
        aliases = item.get("aliases")
        if not isinstance(aliases, list):
            raise ValueError(f"{query_id} gold entity aliases must be a list")

        canonical_aliases = {canonical_value}
        for alias in aliases:
            canonical_alias = _canonical(_required_string(alias, f"{query_id} gold entity alias"))
            canonical_aliases.add(canonical_alias)
        overlap = occupied_aliases & canonical_aliases
        if overlap:
            raise ValueError(f"{query_id} gold entity aliases must identify one entity")
        occupied_aliases.update(canonical_aliases)
        values.add(canonical_value)
        entities.append(
            {
                "value": canonical_value,
                "aliases": sorted(canonical_aliases),
                "reported_value": raw_value,
                "reported_aliases": list(aliases),
            }
        )
    return entities


def _conditions(
    value: object,
    *,
    query_id: str,
    gold_values: set[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(CONDITIONS):
        raise ValueError(f"{query_id} conditions must be exactly full and removed")

    result: dict[str, dict[str, Any]] = {}
    for condition in CONDITIONS:
        raw_condition = value.get(condition)
        if not isinstance(raw_condition, Mapping) or set(raw_condition) != {
            "records",
            "expected_status",
            "supported_entities",
        }:
            raise ValueError(
                f"{query_id}/{condition} must contain records, expected_status, "
                "and supported_entities"
            )
        records = raw_condition.get("records")
        if not isinstance(records, list):
            raise ValueError(f"{query_id}/{condition} records must be a list")
        expected_status = raw_condition.get("expected_status")
        supported = _canonical_list(
            raw_condition.get("supported_entities"),
            f"{query_id}/{condition} supported_entities",
        )
        if not set(supported) <= gold_values:
            raise ValueError(
                f"{query_id}/{condition} supported_entities must be gold-entity values"
            )

        if condition == "full":
            if expected_status != "ANSWERED":
                raise ValueError(f"{query_id}/full expected_status must be ANSWERED")
            if set(supported) != gold_values:
                raise ValueError(f"{query_id}/full supported_entities must match gold_entities")
        else:
            if expected_status not in {"INSUFFICIENT", "PARTIAL"}:
                raise ValueError(
                    f"{query_id}/removed expected_status must be INSUFFICIENT or PARTIAL"
                )
            if expected_status == "INSUFFICIENT" and supported:
                raise ValueError(f"{query_id}/removed INSUFFICIENT must have no support")
            if expected_status == "PARTIAL" and not supported:
                raise ValueError(f"{query_id}/removed PARTIAL must have some support")

        result[condition] = {
            "expected_status": expected_status,
            "supported_entities": supported,
            "records": list(records),
        }
    return result


def _row_index(
    rows: list[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    case_by_query = {case["query_id"]: case for case in cases}
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("each row must be an object")
        query_id = _required_string(row.get("query_id"), "row query_id")
        if query_id in indexed:
            raise ValueError("row query_id values must be unique")
        case = case_by_query.get(query_id)
        if case is None:
            raise ValueError(f"row contains unknown query_id {query_id!r}")
        if row.get("paper_id") != case["paper_id"]:
            raise ValueError(f"row paper_id does not match query_id {query_id!r}")
        indexed[query_id] = row
    return indexed


def _score_case(
    case: Mapping[str, Any],
    row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    conditions: dict[str, dict[str, Any]] = {}
    row_complete = row is not None
    if row is not None:
        row_complete = set(row) == {"query_id", "paper_id", "conditions"}

    for condition in CONDITIONS:
        supported = set(case["conditions"][condition]["supported_entities"])
        expected_status = case["conditions"][condition]["expected_status"]
        group: dict[str, Any] = {
            "expected_status": expected_status,
            "supported_entities": sorted(supported),
            "baseline": None,
            "typed": None,
        }
        raw_conditions = row.get("conditions") if row is not None else None
        if not isinstance(raw_conditions, Mapping):
            raw_conditions = {}
        if set(raw_conditions) != set(CONDITIONS):
            row_complete = False
        raw_group = raw_conditions.get(condition)
        if not isinstance(raw_group, Mapping):
            raw_group = {}
        if set(raw_group) != set(ARMS):
            row_complete = False

        for arm in ARMS:
            raw_result = raw_group.get(arm)
            recorded = arm in raw_group
            if not recorded or not isinstance(raw_result, Mapping):
                row_complete = False
            provided = isinstance(raw_result, Mapping)
            group[arm] = _score_result(
                raw_result,
                recorded=recorded,
                provided=provided,
                arm=arm,
                condition=condition,
                gold_entities=case["gold_entities"],
                supported_values=supported,
                expected_removed_status=expected_status,
                answer_references=case["answer_references"],
                visible_ids={r["source_id"] for r in case["conditions"][condition]["records"]},
            )
        conditions[condition] = group

    return {
        "query_id": case["query_id"],
        "paper_id": case["paper_id"],
        "row_present": row is not None,
        "row_complete": row_complete,
        "answer_references": list(case["answer_references"]),
        "gold_entities": [
            {
                "value": entity["value"],
                "aliases": list(entity["aliases"]),
            }
            for entity in case["gold_entities"]
        ],
        "conditions": conditions,
    }


def _score_result(
    raw_result: Mapping[str, Any] | None,
    *,
    recorded: bool,
    provided: bool,
    arm: str,
    condition: str,
    gold_entities: Sequence[Mapping[str, Any]],
    supported_values: set[str],
    expected_removed_status: str,
    answer_references: Sequence[str],
    visible_ids: set[str],
) -> dict[str, Any]:
    if not recorded:
        result = _not_attempted_result()
        structure_valid = False
        reported_status = None
        reported_delivery_status = None
    elif not provided or raw_result is None:
        result = _invalid_result()
        structure_valid = False
        reported_status = None
        reported_delivery_status = None
    else:
        result, structure_valid = _normalize_result(raw_result, arm=arm)
        reported_status = raw_result.get("status")
        reported_delivery_status = raw_result.get("delivery_status")

    status = result["status"]
    structure_valid = structure_valid and set(result["cited_source_ids"]).issubset(visible_ids)
    valid = provided and status == "VALID" and structure_valid
    aliases, gold_values = _alias_index(gold_entities)
    response_values = {
        _canonical(value)
        for value in result["answer_values"]
        if isinstance(value, str) and value.strip()
    }
    if condition == "full":
        metrics = _full_metrics(
            response_values,
            gold_values=gold_values,
            aliases=aliases,
            answer_values=result["answer_values"],
            answer_references=answer_references,
            delivery_status=result["delivery_status"],
            valid=valid,
        )
    else:
        metrics = _removed_metrics(
            {aliases.get(value, value) for value in response_values},
            supported_values=supported_values,
            delivery_status=result["delivery_status"],
            expected_status=expected_removed_status,
            arm=arm,
            valid=valid,
        )

    return {
        "recorded": recorded,
        "provided": provided,
        "structure_valid": structure_valid,
        "valid": valid,
        "reported_status": reported_status,
        "reported_delivery_status": reported_delivery_status,
        "result": result,
        "metrics": metrics,
    }


def _normalize_result(
    raw_result: Mapping[str, Any],
    *,
    arm: str,
) -> tuple[dict[str, Any], bool]:
    reported_status = raw_result.get("status")
    status = (
        reported_status
        if type(reported_status) is str and reported_status in RESULT_STATUSES
        else "INVALID_OUTPUT"
    )
    delivery_status = raw_result.get("delivery_status")
    if type(delivery_status) is not str or delivery_status not in DELIVERY_STATUSES:
        delivery_status = None
    answer_values = raw_result.get("answer_values")
    if not isinstance(answer_values, list) or any(
        type(value) is not str for value in answer_values
    ):
        answer_values = []
    explanation = raw_result.get("explanation")
    if not isinstance(explanation, str):
        explanation = ""
    cited_source_ids = raw_result.get("cited_source_ids")
    if not isinstance(cited_source_ids, list) or any(
        type(source_id) is not str for source_id in cited_source_ids
    ):
        cited_source_ids = []
    usage = raw_result.get("usage")
    if not valid_usage(usage):
        usage = None
    latency_ms = raw_result.get("latency_ms")
    if not valid_latency(latency_ms):
        latency_ms = None

    result = {
        "status": status,
        "delivery_status": delivery_status,
        "answer_values": list(answer_values),
        "explanation": explanation,
        "cited_source_ids": list(cited_source_ids),
        "usage": usage,
        "latency_ms": latency_ms,
    }
    return result, result_structure_valid(raw_result, arm=arm, status=status)


def result_structure_valid(
    result: Mapping[str, Any],
    *,
    arm: str,
    status: object | None = None,
) -> bool:
    """Return whether one normalized result satisfies the result contract."""

    if not RESULT_FIELDS.issubset(result):
        return False
    if status is None:
        status = result.get("status")
    if type(status) is not str or status not in RESULT_STATUSES:
        return False
    delivery_status = result.get("delivery_status")
    answer_values = result.get("answer_values")
    explanation = result.get("explanation")
    cited_source_ids = result.get("cited_source_ids")
    if delivery_status is not None and (
        type(delivery_status) is not str or delivery_status not in DELIVERY_STATUSES
    ):
        return False
    if (
        not isinstance(answer_values, list)
        or any(type(value) is not str or not value.strip() for value in answer_values)
        or type(explanation) is not str
        or not isinstance(cited_source_ids, list)
        or any(type(source_id) is not str or not source_id for source_id in cited_source_ids)
        or len(cited_source_ids) != len(set(cited_source_ids))
    ):
        return False
    if not valid_usage(result.get("usage")):
        return False
    if not valid_latency(result.get("latency_ms")):
        return False
    if status == "VALID":
        if delivery_status is None:
            return False
        if delivery_status in {"ANSWERED", "PARTIAL"} and not answer_values:
            return False
        if delivery_status in {"INSUFFICIENT", "NEEDS_CLARIFICATION"} and answer_values:
            return False
    if arm == "baseline" and delivery_status == "PARTIAL":
        return False
    return True


def _full_metrics(
    response_values: set[str],
    *,
    gold_values: set[str],
    aliases: Mapping[str, str],
    answer_values: Sequence[str],
    answer_references: Sequence[str],
    delivery_status: str | None,
    valid: bool,
) -> dict[str, Any]:
    predicted_entities = {aliases.get(value, value) for value in response_values}
    true_positives = len(predicted_entities & gold_values)
    false_positives = len(predicted_entities - gold_values)
    false_negatives = len(gold_values - predicted_entities)
    precision = _ratio(true_positives, true_positives + false_positives)
    recall = _ratio(true_positives, true_positives + false_negatives)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else 0.0
    )
    value_set_exact = predicted_entities == gold_values
    set_exact = valid and value_set_exact
    strict_primary_success = set_exact and delivery_status == "ANSWERED"
    quality = (
        answer_metrics(" ".join(answer_values), list(answer_references))
        if valid
        else {"exact_match": 0, "token_f1": 0.0}
    )
    if not valid:
        true_positives = false_positives = 0
        false_negatives = len(gold_values)
        precision = recall = f1 = 0.0
    return {
        "answer_exact_match": quality["exact_match"],
        "answer_token_f1": quality["token_f1"],
        "entity_true_positives": true_positives,
        "entity_false_positives": false_positives,
        "entity_false_negatives": false_negatives,
        "entity_precision": precision if precision is not None else 0.0,
        "entity_recall": recall if recall is not None else 0.0,
        "entity_f1": f1,
        "value_set_exact": value_set_exact,
        "entity_set_exact": set_exact,
        "strict_primary_success": strict_primary_success,
        "canonical_answer_values": sorted(response_values),
        "canonical_entity_values": sorted(predicted_entities),
        "unexpected_values": sorted(predicted_entities - gold_values),
    }


def _removed_metrics(
    predicted_entities: set[str],
    *,
    supported_values: set[str],
    delivery_status: str | None,
    expected_status: str,
    arm: str,
    valid: bool,
) -> dict[str, Any]:
    matched = predicted_entities & supported_values if valid else set()
    outside = predicted_entities - supported_values
    coverage = _ratio(len(matched), len(supported_values)) if supported_values and valid else None
    safe_refusal = bool(
        valid
        and delivery_status == "INSUFFICIENT"
        and not predicted_entities
        and (
            (expected_status == "INSUFFICIENT" and not supported_values)
            or (expected_status == "PARTIAL" and bool(supported_values))
        )
    )
    safe_partial = bool(
        valid
        and arm == "typed"
        and expected_status == "PARTIAL"
        and delivery_status == "PARTIAL"
        and bool(predicted_entities)
        and not outside
    )
    wrong_full = delivery_status == "ANSWERED" or bool(outside)
    return {
        "entity_value_coverage": coverage if coverage is not None else 0.0,
        "supported_entity_count": len(supported_values),
        "matched_entity_count": len(matched),
        "matched_supported_values": sorted(matched),
        "out_of_support_values": sorted(outside),
        "safe_refusal": safe_refusal,
        "safe_partial_delivery": safe_partial,
        "safe_delivery": safe_refusal or safe_partial,
        "wrong_full_delivery": wrong_full,
    }


def _aggregate(
    cases: Sequence[Mapping[str, Any]],
    scored_cases: Sequence[Mapping[str, Any]],
    *,
    ledger_size: int,
) -> dict[str, Any]:
    condition_reports: dict[str, dict[str, Any]] = {}
    for condition in CONDITIONS:
        arm_reports = {
            arm: _arm_report(
                [case["conditions"][condition][arm] for case in scored_cases],
                condition=condition,
            )
            for arm in ARMS
        }
        condition_reports[condition] = {
            "planned": len(cases),
            "arms": arm_reports,
        }

    condition_reports["full"]["paper_cluster_diagnostics"] = _paper_cluster_diagnostics(
        scored_cases
    )
    pooled_results = [
        case["conditions"][condition][arm]
        for case in scored_cases
        for condition in CONDITIONS
        for arm in ARMS
    ]
    usage_by_condition = {
        condition: {
            arm: _usage_report([case["conditions"][condition][arm] for case in scored_cases])
            for arm in ARMS
        }
        for condition in CONDITIONS
    }
    return {
        "planned_cases": len(cases),
        "ledger_proposals": ledger_size,
        "conditions": condition_reports,
        "usage": {
            "pooled": _usage_report(pooled_results),
            "conditions": usage_by_condition,
            "currency_cost": None,
        },
        "limitations": [
            "Entity aliases are scored by casefold, NFC, and whitespace normalization only.",
            "Entity values must match one complete canonical alias; unexpected values are "
            "false positives.",
            "The full primary metric verifies only exact alias-set delivery and reported "
            "ANSWERED status.",
            "Model-reported relation and completeness are not verified semantic judgments.",
            "Short-value exact match and token F1 join answer_values in provided order and use "
            "the original references; explanations are excluded.",
            "The paper-cluster interval is descriptive and cannot promote a method automatically.",
        ],
        "automatic_promotion": False,
        "semantic_entailment_verified": False,
    }


def _arm_report(results: Sequence[Mapping[str, Any]], *, condition: str) -> dict[str, Any]:
    planned = len(results)
    valid = [result for result in results if result["valid"]]
    report: dict[str, Any] = {
        "planned": planned,
        "observed": sum(result["provided"] for result in results),
        "observed_denominator": sum(result["provided"] for result in results),
        "attempted": sum(
            result["provided"] and result["result"]["status"] != "NOT_ATTEMPTED"
            for result in results
        ),
        "valid": len(valid),
        "invalid_or_unobserved": planned - len(valid),
        "scoring_denominator": planned,
        "status_counts": _counts(result["result"]["status"] for result in results),
        "reported_status_counts": _reported_status_counts(
            result["reported_status"] for result in results
        ),
        "delivery_status_counts": _delivery_counts(
            result["result"]["delivery_status"] for result in results
        ),
        "formatting_invalid_valid_status": sum(
            result["result"]["status"] == "VALID" and not result["structure_valid"]
            for result in results
        ),
    }
    if condition == "full":
        report.update(_full_arm_report(results, planned=planned))
    else:
        report.update(_removed_arm_report(results, planned=planned))
    return report


def _full_arm_report(
    results: Sequence[Mapping[str, Any]],
    *,
    planned: int,
) -> dict[str, Any]:
    metrics = [result["metrics"] for result in results]
    true_positives = sum(value["entity_true_positives"] for value in metrics)
    false_positives = sum(value["entity_false_positives"] for value in metrics)
    false_negatives = sum(value["entity_false_negatives"] for value in metrics)
    precision = _ratio(true_positives, true_positives + false_positives)
    recall = _ratio(true_positives, true_positives + false_negatives)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else 0.0
    )
    return {
        "answer_exact_match": fmean(value["answer_exact_match"] for value in metrics),
        "answer_token_f1": fmean(value["answer_token_f1"] for value in metrics),
        "entity_precision": precision if precision is not None else 0.0,
        "entity_recall": recall if recall is not None else 0.0,
        "entity_f1": f1,
        "entity_precision_macro": fmean(value["entity_precision"] for value in metrics),
        "entity_recall_macro": fmean(value["entity_recall"] for value in metrics),
        "entity_f1_macro": fmean(value["entity_f1"] for value in metrics),
        "entity_set_exact_rate": _rate(
            sum(value["entity_set_exact"] for value in metrics), planned
        ),
        "strict_primary_success_rate": _rate(
            sum(value["strict_primary_success"] for value in metrics), planned
        ),
        "strict_primary_successes": sum(value["strict_primary_success"] for value in metrics),
        "unexpected_value_count": sum(len(value["unexpected_values"]) for value in metrics),
    }


def _removed_arm_report(
    results: Sequence[Mapping[str, Any]],
    *,
    planned: int,
) -> dict[str, Any]:
    metrics = [result["metrics"] for result in results]
    coverage_values = [
        float(value["entity_value_coverage"])
        for value in metrics
        if value["supported_entity_count"] > 0
    ]
    supported_count = sum(value["supported_entity_count"] for value in metrics)
    matched_count = sum(value["matched_entity_count"] for value in metrics)
    return {
        "safe_delivery_rate": _rate(sum(value["safe_delivery"] for value in metrics), planned),
        "safe_refusal_rate": _rate(sum(value["safe_refusal"] for value in metrics), planned),
        "safe_partial_delivery_rate": _rate(
            sum(value["safe_partial_delivery"] for value in metrics), planned
        ),
        "wrong_full_delivery_rate": _rate(
            sum(value["wrong_full_delivery"] for value in metrics), planned
        ),
        "wrong_full_deliveries": sum(value["wrong_full_delivery"] for value in metrics),
        "value_coverage_macro": fmean(coverage_values) if coverage_values else 0.0,
        "value_coverage_micro": _ratio(matched_count, supported_count) or 0.0,
        "coverage_planned_case_count": len(coverage_values),
        "coverage_matched_values": matched_count,
        "coverage_supported_values": supported_count,
    }


def _paper_cluster_diagnostics(
    scored_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    groups = {case["paper_id"]: case for case in scored_cases}
    complete = all(
        case["conditions"]["full"][arm]["provided"] for case in scored_cases for arm in ARMS
    )
    if not complete:
        status = "MISSING_ARMS"
    elif len(groups) < MIN_CLUSTER_GROUPS:
        status = "INSUFFICIENT_PAPER_CLUSTERS"
    else:
        status = "DESCRIPTIVE_ONLY"

    baseline = {
        case["query_id"]: float(
            case["conditions"]["full"]["baseline"]["metrics"]["strict_primary_success"]
        )
        for case in scored_cases
    }
    typed = {
        case["query_id"]: float(
            case["conditions"]["full"]["typed"]["metrics"]["strict_primary_success"]
        )
        for case in scored_cases
    }
    estimate = fmean(typed[query_id] - baseline[query_id] for query_id in baseline)
    if status != "DESCRIPTIVE_ONLY":
        low = high = None
    else:
        clusters = defaultdict(list)
        for case in scored_cases:
            query_id = case["query_id"]
            clusters[case["paper_id"]].append(typed[query_id] - baseline[query_id])
        cluster_values = list(clusters.values())
        rng = Random(BOOTSTRAP_SEED)
        differences = []
        for _ in range(BOOTSTRAP_SAMPLES):
            sampled = [
                cluster_values[rng.randrange(len(cluster_values))]
                for _ in range(len(cluster_values))
            ]
            differences.append(fmean(value for cluster in sampled for value in cluster))
        low = _quantile(differences, 0.025)
        high = _quantile(differences, 0.975)
    return {
        "status": status,
        "paper_groups": len(groups),
        "minimum_paper_groups": MIN_CLUSTER_GROUPS,
        "metric": "typed minus baseline full entity-set exact success",
        "estimate": estimate,
        "ci95_low": low,
        "ci95_high": high,
        "bootstrap_samples": BOOTSTRAP_SAMPLES if status == "DESCRIPTIVE_ONLY" else 0,
        "no_p_values": True,
        "not_automatic_promotion": True,
    }


def _usage_report(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    known_subtotals: dict[str, int] = {}
    unknown_counts: dict[str, int] = {}
    total_values: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        values = [
            result["result"]["usage"][field]
            for result in results
            if result["result"]["usage"] is not None
            and result["result"]["usage"][field] is not None
        ]
        known_subtotals[field] = sum(values)
        unknown_counts[field] = len(results) - len(values)
        total_values[field] = None if unknown_counts[field] else known_subtotals[field]
    latency = [
        float(result["result"]["latency_ms"])
        for result in results
        if result["result"]["latency_ms"] is not None
    ]
    return {
        "observed_results": len(results),
        "known_subtotals": known_subtotals,
        "totals": total_values,
        "unknown_counts": unknown_counts,
        "known_counts": {field: len(results) - unknown_counts[field] for field in USAGE_FIELDS},
        "latency_ms": {
            "observed": len(latency),
            "unknown": len(results) - len(latency),
            "total": sum(latency),
            "mean": fmean(latency) if latency else None,
            "p50": _quantile(latency, 0.5),
            "p95": _quantile(latency, 0.95),
        },
        "money": None,
    }


def _alias_index(
    gold_entities: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    values: set[str] = set()
    for entity in gold_entities:
        value = entity["value"]
        values.add(value)
        for alias in entity["aliases"]:
            aliases[alias] = value
    return aliases, values


def _canonical_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    canonical = [_canonical(_required_string(item, field)) for item in value]
    if len(canonical) != len(set(canonical)):
        raise ValueError(f"{field} must be unique after normalization")
    return canonical


def _canonical(value: str) -> str:
    normalized = unicode_normalize("NFC", value).casefold()
    normalized = unicode_normalize("NFC", normalized)
    return " ".join(normalized.split())


def _required_string(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _not_attempted_result() -> dict[str, Any]:
    return {
        "status": "NOT_ATTEMPTED",
        "delivery_status": None,
        "answer_values": [],
        "explanation": "",
        "cited_source_ids": [],
        "usage": None,
        "latency_ms": None,
    }


def _invalid_result() -> dict[str, Any]:
    result = _not_attempted_result()
    result["status"] = "INVALID_OUTPUT"
    return result


def valid_usage(value: object) -> bool:
    if value is None:
        return True
    if (
        not isinstance(value, Mapping)
        or not set(USAGE_FIELDS).issubset(value)
        or set(value) - {*USAGE_FIELDS, "cached_tokens"}
    ):
        return False
    cached = value.get("cached_tokens")
    if cached is not None and (type(cached) is not int or cached < 0):
        return False
    for field in USAGE_FIELDS:
        item = value[field]
        if item is not None and (type(item) is not int or item < 0):
            return False
    input_tokens, output_tokens, total_tokens = (value[field] for field in USAGE_FIELDS)
    return (
        total_tokens is None
        or input_tokens is None
        or output_tokens is None
        or total_tokens == input_tokens + output_tokens
    )


def valid_latency(value: object) -> bool:
    return value is None or (type(value) in {int, float} and isfinite(value) and value >= 0)


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _delivery_counts(values: Iterable[str | None]) -> dict[str, int]:
    return dict(sorted(Counter("NONE" if value is None else value for value in values).items()))


def _reported_status_counts(values: Iterable[object]) -> dict[str, int]:
    return dict(
        sorted(Counter("NONE" if value is None else str(value) for value in values).items())
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


__all__ = ["score_delivery"]
