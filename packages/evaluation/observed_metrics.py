"""v2 evaluators add availability guards while retaining v1 historical evaluators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.evaluation.metrics import (
    MetricStatus,
    MetricValue,
    _mrr,
    _not_available,
    _recall,
    _unique,
)


def observed_metrics(
    category: str,
    expected: Mapping[str, Any],
    observation: Mapping[str, Any],
    legacy: dict[str, MetricValue],
) -> dict[str, MetricValue]:
    metrics = dict(legacy)
    requirements = {
        "TOOL": {
            "tool_selection_accuracy": ("tool_identity", "observed_tool_identity"),
            "tool_argument_accuracy": ("arguments", "arguments_hash"),
            "tool_sequence_accuracy": ("tool_sequence",),
        },
        "APPROVAL": {
            "approval_required_accuracy": ("approval_required",),
            "approval_decision_accuracy": ("approval_decision", "decision"),
            "denied_action_execution_rate": ("action_executed",),
            "unauthorized_execution_rate": ("unauthorized_execution",),
            "duplicate_side_effect_rate": ("duplicate_side_effect",),
            "unknown_outcome_semantics_accuracy": ("unknown_outcome_semantics_ok",),
        },
        "MULTI_STEP": {
            "tool_sequence_accuracy": ("steps", "tool_sequence"),
            "required_steps_covered": ("steps", "tool_sequence"),
            "terminal_task_success": ("terminal_status",),
        },
        "KNOWLEDGE_QA": {
            "citation_precision": ("citation_ids",),
            "citation_coverage": ("citation_ids",),
        },
        "FAILURE": {"task_success": ("observed_agent_status", "status")},
    }.get(category, {})
    missing_required = False
    for name, keys in requirements.items():
        if name not in metrics:
            continue
        if (
            name == "tool_sequence_accuracy"
            and category == "TOOL"
            and "tool_sequence" not in expected
        ):
            continue
        if name == "terminal_task_success" and "terminal_status" not in expected:
            continue
        if (
            name == "unknown_outcome_semantics_accuracy"
            and "unknown_outcome_semantics_ok" not in observation
        ):
            continue
        if not any(key in observation and observation[key] is not None for key in keys):
            metrics[name] = _not_available(name, "execution_observation_missing")
            missing_required = True
    for field, metric_name in (
        ("action_executed", "denied_action_execution_rate"),
        ("unauthorized_execution", "unauthorized_execution_rate"),
        ("duplicate_side_effect", "duplicate_side_effect_rate"),
        ("approval_required", "approval_required_accuracy"),
    ):
        if category == "APPROVAL" and type(observation.get(field)) is not bool:
            metrics[metric_name] = _not_available(metric_name, "boolean_observation_missing")
            missing_required = True
    if category == "NO_ANSWER" and type(observation.get("answerable")) is not bool:
        metrics["task_success"] = _not_available("task_success", "boolean_observation_missing")
    if category == "KNOWLEDGE_QA" and not any(
        observation.get(key) is not None for key in ("answer", "answer_hash")
    ):
        for name in ("answer_correctness", "task_success"):
            metrics[name] = _not_available(name, "answer_observation_missing")
    if (
        category == "FAILURE"
        and expected.get("failure_code") is not None
        and not any(
            observation.get(key) is not None
            for key in ("observed_agent_failure_code", "failure_code")
        )
    ):
        missing_required = True
    if missing_required and category in {"TOOL", "APPROVAL", "MULTI_STEP", "FAILURE"}:
        metrics["task_success"] = _not_available("task_success", "required_observation_missing")
    if category == "RETRIEVAL":
        relevant = _unique(expected.get("relevant_chunk_ids", expected.get("relevant_ids")))
        for field, fixed_name, dynamic_name, limit in (
            (
                "candidate_chunk_ids",
                "candidate_recall_at_20",
                "candidate_recall_at_configured_k",
                20,
            ),
            ("final_chunk_ids", "final_recall_at_5", "final_recall_at_configured_k", 5),
        ):
            k_key = "candidate_top_k" if field.startswith("candidate") else "final_top_k"
            k = observation.get(k_key)
            found = observation.get(field)
            valid = isinstance(found, list) and all(isinstance(value, str) for value in found)
            if not valid:
                metrics[fixed_name] = _not_available(fixed_name, "retrieval_stage_missing")
                metrics[dynamic_name] = _not_available(dynamic_name, "retrieval_stage_missing")
                if field == "final_chunk_ids":
                    metrics["mrr_at_5"] = _not_available("mrr_at_5", "retrieval_stage_missing")
                    metrics["mrr_at_configured_k"] = _not_available(
                        "mrr_at_configured_k", "retrieval_stage_missing"
                    )
                    metrics["task_success"] = _not_available(
                        "task_success", "retrieval_stage_missing"
                    )
                continue
            ids = _unique(found)
            metrics[fixed_name] = _recall(fixed_name, relevant, ids[:limit])
            if type(k) is int and k > 0:
                metrics[dynamic_name] = _recall(dynamic_name, relevant, ids[:k])
                if k < limit:
                    metrics[fixed_name] = _not_available(fixed_name, "configured_k_below_metric_k")
                if field == "final_chunk_ids":
                    metrics["mrr_at_configured_k"] = _mrr("mrr_at_configured_k", relevant, ids[:k])
                    metrics["mrr_at_5"] = (
                        _mrr("mrr_at_5", relevant, ids[:5])
                        if k >= 5
                        else _not_available("mrr_at_5", "configured_k_below_metric_k")
                    )
            else:
                metrics[dynamic_name] = _not_available(dynamic_name, "configured_k_missing")
                metrics[fixed_name] = _not_available(fixed_name, "configured_k_missing")
                if field == "final_chunk_ids":
                    for name in ("mrr_at_5", "mrr_at_configured_k"):
                        metrics[name] = _not_available(name, "configured_k_missing")
        for name in ("citation_precision", "citation_coverage"):
            if "citation_ids" not in observation:
                metrics[name] = _not_available(name, "citation_observation_missing")
        if not relevant:
            metrics["task_success"] = MetricValue(
                "task_success", MetricStatus.NOT_APPLICABLE, None, 0, reason="ground_truth_empty"
            )
    return metrics
