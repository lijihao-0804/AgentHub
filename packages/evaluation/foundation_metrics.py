"""Additive v2 timing, business and cost summaries using existing metric primitives."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from packages.evaluation.metrics import (
    MetricStatus,
    MetricValue,
    _not_available,
    percentile,
)


def _finite(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def foundation_metrics(rows: Sequence[tuple[Any, Any, Any]]) -> dict[str, MetricValue]:
    business = [
        value if isinstance(value := case.observation.get("business_outcome"), dict) else {}
        for case, _, _ in rows
    ]
    known = [
        value
        for value in business
        if value.get("status") == "AVAILABLE" and type(value.get("success")) is bool
    ]
    success = sum(value["success"] for value in known)
    output = {
        "business_task_success_rate": (
            MetricValue(
                "business_task_success_rate",
                MetricStatus.AVAILABLE,
                success / len(rows),
                len(known),
                success,
                len(rows),
                evaluator_version="v2",
                details={"unknown_count": len(rows) - len(known), "unit": "case_execution"},
            )
            if known
            else _not_available(
                "business_task_success_rate", "business_postconditions_missing", version="v2"
            )
        )
    }
    timings: dict[str, list[float]] = defaultdict(list)
    for case, _, _ in rows:
        timing = case.observation.get("timing", {})
        if not isinstance(timing, dict):
            continue
        value = timing.get("server_ttft_ms")
        if _finite(value):
            timings["server_ttft"].append(float(value))
        stages = timing.get("stage_durations_ms", {})
        if not isinstance(stages, dict):
            continue
        for name, durations in stages.items():
            # Frozen vocabulary only: no unbounded metric names from external input.
            if isinstance(durations, list) and name in {
                "model.generate",
                "tool.execute",
                "dense",
                "sparse",
                "fusion",
                "rerank",
            }:
                timings["stage_" + name.replace(".", "_")].extend(
                    float(v) for v in durations if _finite(v)
                )
    for prefix in {
        "server_ttft",
        "stage_model_generate",
        "stage_tool_execute",
        "stage_dense",
        "stage_sparse",
        "stage_fusion",
        "stage_rerank",
    }:
        values = timings[prefix]
        for suffix, rank in (("p50_ms", 0.5), ("p95_ms", 0.95)):
            name = f"{prefix}_{suffix}"
            output[name] = (
                MetricValue(
                    name,
                    MetricStatus.AVAILABLE,
                    percentile(values, rank),
                    len(values),
                    evaluator_version="v2",
                )
                if values
                else _not_available(name, "timing_not_observed", version="v2")
            )
    costs: dict[str, list[Decimal]] = defaultdict(list)
    successes_by_currency: dict[str, int] = defaultdict(int)
    for index, (case, _, _) in enumerate(rows):
        if (
            case.cost_currency
            and business[index].get("success") is True
            and business[index].get("status") == "AVAILABLE"
        ):
            successes_by_currency[case.cost_currency] += 1
        if case.cost_amount is not None and case.cost_currency:
            value = Decimal(str(case.cost_amount))
            if value.is_finite() and value >= 0:
                costs[case.cost_currency].append(value)
    for currency, amounts in costs.items():
        name = f"effective_task_cost_{currency}"
        denominator = successes_by_currency[currency]
        output[name] = (
            MetricValue(
                name,
                MetricStatus.AVAILABLE,
                sum(amounts) / denominator,
                len(amounts),
                sum(amounts),
                denominator,
                evaluator_version="v2",
                details={
                    "unknown_cost_count": len(rows) - sum(map(len, costs.values())),
                    "unit": "business_successful_case_execution",
                    "includes_failed_attempts": True,
                    "partial_known_cost": sum(map(len, costs.values())) < len(rows),
                    "unknown_business_count": len(rows) - len(known),
                },
            )
            if denominator
            else _not_available(name, "no_business_success", version="v2")
        )
    output["case_cost_coverage"] = (
        MetricValue(
            "case_cost_coverage",
            MetricStatus.AVAILABLE,
            sum(map(len, costs.values())) / len(rows),
            len(rows),
            sum(map(len, costs.values())),
            len(rows),
            evaluator_version="v2",
        )
        if rows
        else _not_available("case_cost_coverage", "no_cases", version="v2")
    )
    for kind in ("runtime", "controlled", "synthetic", "unknown"):
        name = f"observation_{kind}_case_count"
        count = sum(
            (
                case.observation.get("driver_kind")
                if isinstance(case.observation.get("driver_kind"), str)
                and case.observation.get("driver_kind") in {"runtime", "controlled", "synthetic"}
                else "unknown"
            )
            == kind
            for case, _, _ in rows
        )
        output[name] = MetricValue(
            name, MetricStatus.AVAILABLE, count, len(rows), evaluator_version="v2"
        )
    billing = [
        case.observation.get("timing", {}).get("billing")
        for case, _, _ in rows
        if isinstance(case.observation.get("timing"), dict)
    ]
    billing = [
        b
        for b in billing
        if isinstance(b, dict)
        and b.get("collection_complete") is True
        and type(b.get("provider_attempt_count")) is int
        and b["provider_attempt_count"] >= 0
        and all(
            type(b.get(field)) is int and 0 <= b[field] <= b["provider_attempt_count"]
            for field in ("known_usage_count", "known_cost_count")
        )
    ]
    attempts = sum(b["provider_attempt_count"] for b in billing)
    for field in ("usage", "cost"):
        name = f"provider_{field}_coverage"
        numerator = sum(b[f"known_{field}_count"] for b in billing)
        output[name] = (
            MetricValue(
                name,
                MetricStatus.AVAILABLE,
                numerator / attempts,
                len(billing),
                numerator,
                attempts,
                evaluator_version="v2",
                details={"case_collection_count": len(billing), "case_count": len(rows)},
            )
            if attempts
            else _not_available(name, "provider_attempts_not_observed", version="v2")
        )
    # Case-level costs can exist even when a retry's provider bill is unknown.
    # Keep the known subtotal useful while explicitly reporting that boundary.
    complete_provider_cost = (
        bool(attempts)
        and len(billing) == len(rows)
        and all(b["known_cost_count"] == b["provider_attempt_count"] for b in billing)
    )
    for name, metric in output.items():
        if name.startswith("effective_task_cost_") and metric.status == MetricStatus.AVAILABLE:
            metric.details["provider_cost_complete"] = complete_provider_cost
            metric.details["partial_known_cost"] |= not complete_provider_cost
    return output
