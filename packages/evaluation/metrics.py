"""Deterministic M7-D evaluator contracts and metric calculations."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from statistics import mean
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash


class MetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    UNKNOWN = "UNKNOWN"


class MetricAggregationKind(StrEnum):
    BINARY = "BINARY"
    RATE = "RATE"
    SCALAR = "SCALAR"
    COST = "COST"


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    direction: MetricDirection
    aggregation_kind: MetricAggregationKind
    task_success_relevant: bool = False
    version: str = "v1"


@dataclass(frozen=True)
class MetricValue:
    name: str
    status: MetricStatus
    value: Decimal | float | int | None
    sample_count: int
    numerator: Decimal | float | int | None = None
    denominator: Decimal | float | int | None = None
    reason: str | None = None
    evaluator_version: str = "v1"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "value": _json_number(self.value),
            "sample_count": self.sample_count,
            "numerator": _json_number(self.numerator),
            "denominator": _json_number(self.denominator),
            "reason": self.reason,
            "evaluator_version": self.evaluator_version,
            "details": self.details,
        }


@dataclass(frozen=True)
class PairedMetric:
    baseline: MetricValue
    candidate: MetricValue
    absolute_delta: float | None
    relative_delta: float | None
    paired_win: int
    paired_tie: int
    paired_loss: int
    applicable_pairs: int
    missing_pairs: int
    direction: MetricDirection
    status: str = "COMPLETE"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "absolute_delta": self.absolute_delta,
            "relative_delta": self.relative_delta,
            "paired_win": self.paired_win,
            "paired_tie": self.paired_tie,
            "paired_loss": self.paired_loss,
            "applicable_pairs": self.applicable_pairs,
            "missing_pairs": self.missing_pairs,
            "direction": str(self.direction),
            "status": self.status,
            "reason": self.reason,
        }


Evaluator = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, MetricValue]]


class EvaluatorRegistry:
    """Versioned category registry bound to the experiment's frozen manifest."""

    def __init__(self) -> None:
        self._evaluators: dict[str, tuple[str, Evaluator]] = {}
        self._definitions: dict[str, MetricDefinition] = dict(METRIC_DEFINITIONS)
        self.register("RETRIEVAL", "v1", evaluate_retrieval)
        self.register("KNOWLEDGE_QA", "v1", evaluate_knowledge_qa)
        self.register("TOOL", "v1", evaluate_tool)
        self.register("NO_ANSWER", "v1", evaluate_no_answer)
        self.register("APPROVAL", "v1", evaluate_approval)
        self.register("MULTI_STEP", "v1", evaluate_multi_step)
        self.register("FAILURE", "v1", evaluate_failure)

    def register(self, category: str, version: str, evaluator: Evaluator) -> None:
        self._evaluators[category] = (version, evaluator)

    def version_for(self, category: str) -> str:
        return self._evaluators[category][0]

    def register_metric_definition(
        self,
        name: str,
        direction: MetricDirection,
        aggregation_kind: MetricAggregationKind,
        *,
        task_success_relevant: bool = False,
        version: str = "v1",
    ) -> None:
        self._definitions[name] = MetricDefinition(
            name=name,
            direction=direction,
            aggregation_kind=aggregation_kind,
            task_success_relevant=task_success_relevant,
            version=version,
        )

    def definition_for(self, name: str) -> MetricDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"UNKNOWN_METRIC_DEFINITION:{name}") from exc

    def direction_for(self, name: str) -> MetricDirection:
        return self.definition_for(name).direction

    def version_for_metric(self, name: str) -> str:
        return self.definition_for(name).version

    def validate_manifest(self, manifest: Mapping[str, Any]) -> None:
        versions = manifest.get("evaluator_versions")
        if not isinstance(versions, Mapping):
            raise ValueError("EXPERIMENT_EVALUATOR_VERSION_MISMATCH")
        for category, (version, _) in self._evaluators.items():
            key = _manifest_key(category)
            if versions.get(key) != version:
                raise ValueError("EXPERIMENT_EVALUATOR_VERSION_MISMATCH")

    def evaluate(
        self,
        category: str,
        expected: Mapping[str, Any],
        observation: Mapping[str, Any],
    ) -> dict[str, MetricValue]:
        try:
            version, evaluator = self._evaluators[category]
        except KeyError:
            return {
                "task_success": MetricValue(
                    "task_success",
                    MetricStatus.NOT_AVAILABLE,
                    None,
                    0,
                    reason="unsupported_category",
                )
            }
        return {
            name: _with_version(metric, version)
            for name, metric in evaluator(expected, observation).items()
        }


def evaluate_case(
    category: str,
    expected: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    registry: EvaluatorRegistry | None = None,
) -> dict[str, MetricValue]:
    return (registry or EvaluatorRegistry()).evaluate(category, expected, observation)


def evaluate_tool(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    expected_tool = expected.get("tool_identity")
    observed_tool = observation.get("tool_identity") or observation.get("observed_tool_identity")
    expected_args = expected.get("arguments", {})
    observed_args = observation.get("arguments")
    if observed_args is None and "arguments_hash" in observation:
        observed_args = observation["arguments_hash"]
        expected_args = canonical_json_hash(expected_args)
    else:
        observed_args = canonical_json_hash(observed_args) if observed_args is not None else None
        expected_args = canonical_json_hash(expected_args)
    selection = _binary_metric("tool_selection_accuracy", expected_tool == observed_tool)
    args = _binary_metric("tool_argument_accuracy", expected_args == observed_args)
    expected_sequence = expected.get("tool_sequence")
    observed_sequence = observation.get("tool_sequence")
    sequence = _binary_metric(
        "tool_sequence_accuracy",
        expected_sequence == observed_sequence,
        applicable=expected_sequence is not None,
    )
    return {
        "tool_selection_accuracy": selection,
        "tool_argument_accuracy": args,
        "tool_sequence_accuracy": sequence,
        "task_success": _task_success([selection, args, sequence]),
    }


def evaluate_approval(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    required = _binary_metric(
        "approval_required_accuracy",
        bool(expected.get("approval_required")) == bool(observation.get("approval_required")),
    )
    decision = _binary_metric(
        "approval_decision_accuracy",
        str(expected.get("decision", "")).upper()
        == str(observation.get("approval_decision", observation.get("decision", ""))).upper(),
    )
    denied_execution = _rate_metric(
        "denied_action_execution_rate",
        int(
            bool(
                str(expected.get("decision", "")).upper() == "DENIED"
                and observation.get("action_executed")
            )
        ),
        1,
    )
    unauthorized = _rate_metric(
        "unauthorized_execution_rate", int(bool(observation.get("unauthorized_execution"))), 1
    )
    duplicate = _rate_metric(
        "duplicate_side_effect_rate", int(bool(observation.get("duplicate_side_effect"))), 1
    )
    unknown = _binary_metric(
        "unknown_outcome_semantics_accuracy",
        observation.get("unknown_outcome_semantics_ok") is True,
        applicable=observation.get("unknown_outcome_semantics_ok") is not None,
    )
    return {
        "approval_required_accuracy": required,
        "approval_decision_accuracy": decision,
        "denied_action_execution_rate": denied_execution,
        "unauthorized_execution_rate": unauthorized,
        "duplicate_side_effect_rate": duplicate,
        "unknown_outcome_semantics_accuracy": unknown,
        "task_success": _task_success(
            [required, decision, denied_execution, unauthorized, duplicate, unknown],
            zero_is_success={
                "denied_action_execution_rate",
                "unauthorized_execution_rate",
                "duplicate_side_effect_rate",
            },
        ),
    }


def evaluate_failure(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    status_ok = (
        str(expected.get("status", "")).upper()
        == str(observation.get("observed_agent_status", observation.get("status", ""))).upper()
    )
    code_ok = expected.get("failure_code") == observation.get(
        "observed_agent_failure_code", observation.get("failure_code")
    )
    return {"task_success": _binary_metric("task_success", status_ok and code_ok)}


def evaluate_retrieval(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    relevant = _unique(expected.get("relevant_chunk_ids", expected.get("relevant_ids", [])))
    candidate = _unique(observation.get("candidate_chunk_ids", observation.get("chunk_ids", [])))
    final = _unique(observation.get("final_chunk_ids", candidate))
    expected_citations = _unique(
        expected.get("citation_chunk_ids", expected.get("citations", relevant))
    )
    observed_citations = _unique(observation.get("citation_ids"))
    citation_precision = _precision("citation_precision", expected_citations, observed_citations)
    citation_coverage = _recall("citation_coverage", expected_citations, observed_citations)
    return {
        "candidate_recall_at_20": _recall("candidate_recall_at_20", relevant, candidate[:20]),
        "final_recall_at_5": _recall("final_recall_at_5", relevant, final[:5]),
        "mrr_at_5": _mrr("mrr_at_5", relevant, final[:5]),
        "citation_precision": citation_precision,
        "citation_coverage": citation_coverage,
        "task_success": _binary_metric("task_success", set(relevant).issubset(final)),
    }


def evaluate_knowledge_qa(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    accepted = expected.get("accepted_answers")
    if isinstance(accepted, Sequence) and not isinstance(accepted, (str, bytes)):
        expected_hashes = {
            str(value) if len(str(value)) == 64 else canonical_json_hash(value)
            for value in accepted
        }
    else:
        expected_hashes = {
            expected.get("answer_hash") or canonical_json_hash(expected.get("answer", ""))
        }
    observed_hash = observation.get("answer_hash")
    if observed_hash is None and "answer" in observation:
        observed_hash = canonical_json_hash(observation["answer"])
    answer = _binary_metric(
        "answer_correctness",
        observed_hash in expected_hashes if observed_hash is not None else False,
        applicable=observed_hash is not None,
    )
    expected_citations = _unique(expected.get("citations", expected.get("relevant_chunk_ids", [])))
    observed_citations = _unique(observation.get("citation_ids"))
    return {
        "answer_correctness": answer,
        "citation_precision": _precision(
            "citation_precision", expected_citations, observed_citations
        ),
        "citation_coverage": _recall("citation_coverage", expected_citations, observed_citations),
        "faithfulness": evaluate_faithfulness(expected, observation),
        "task_success": answer,
    }


def evaluate_no_answer(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    signal = observation.get("answerable")
    if signal is None:
        task = _not_available("task_success", "structured_answerability_signal_missing")
    else:
        task = _binary_metric("task_success", bool(expected.get("answerable")) == bool(signal))
    return {"task_success": task}


def evaluate_multi_step(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    expected_steps = expected.get("steps", [])
    observed_steps = observation.get("steps", observation.get("tool_sequence"))
    sequence = _binary_metric(
        "tool_sequence_accuracy",
        expected_steps == observed_steps,
        applicable=observed_steps is not None,
    )
    covered = _rate_metric(
        "required_steps_covered",
        len(set(expected_steps).intersection(set(observed_steps or []))),
        len(set(expected_steps)),
    )
    terminal = _binary_metric(
        "terminal_task_success",
        expected.get("terminal_status") == observation.get("terminal_status"),
        applicable=expected.get("terminal_status") is not None,
    )
    return {
        "tool_sequence_accuracy": sequence,
        "required_steps_covered": covered,
        "terminal_task_success": terminal,
        "task_success": _task_success([sequence, covered, terminal]),
    }


def evaluate_faithfulness(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> MetricValue:
    del expected
    if not isinstance(observation.get("evidence_support"), bool):
        return _not_available("faithfulness", "structured_evidence_support_missing")
    return _binary_metric("faithfulness", observation["evidence_support"])


def aggregate_metric_values(
    name: str, values: Sequence[MetricValue], *, version: str = "v1"
) -> MetricValue:
    available = [metric for metric in values if metric.status == MetricStatus.AVAILABLE]
    if not available:
        statuses = {metric.status for metric in values}
        if statuses and statuses <= {MetricStatus.NOT_APPLICABLE}:
            return _not_applicable(name, "not_applicable", version=version)
        return _not_available(name, "no_available_samples", version=version)
    numeric = [float(metric.value) for metric in available if metric.value is not None]
    numerator = sum(float(metric.numerator) for metric in available if metric.numerator is not None)
    denominator = sum(
        float(metric.denominator) for metric in available if metric.denominator is not None
    )
    if denominator:
        value = numerator / denominator
    elif numeric:
        value = mean(numeric)
    else:
        return _not_available(name, "metric_value_missing", version=version)
    counts = {
        "available_count": len(available),
        "not_available_count": sum(
            metric.status == MetricStatus.NOT_AVAILABLE for metric in values
        ),
        "not_applicable_count": sum(
            metric.status == MetricStatus.NOT_APPLICABLE for metric in values
        ),
    }
    return MetricValue(
        name=name,
        status=MetricStatus.AVAILABLE,
        value=value,
        sample_count=len(available),
        numerator=numerator if denominator else None,
        denominator=denominator if denominator else None,
        evaluator_version=version,
        details=counts,
    )


def percentile(values: Sequence[float], percentile_rank: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_rank
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def compare_metric_values(
    baseline: MetricValue,
    candidate: MetricValue,
    *,
    direction: MetricDirection,
    paired: Sequence[tuple[float | None, float | None]] = (),
) -> PairedMetric:
    baseline_value = _float_value(baseline)
    candidate_value = _float_value(candidate)
    absolute = (
        candidate_value - baseline_value
        if baseline_value is not None and candidate_value is not None
        else None
    )
    relative = (
        absolute / baseline_value
        if absolute is not None and baseline_value not in {None, 0}
        else None
    )
    wins = ties = losses = missing = 0
    for before, after in paired:
        if before is None or after is None:
            missing += 1
            continue
        if after == before:
            ties += 1
        elif (after > before) == (direction == MetricDirection.HIGHER_IS_BETTER):
            wins += 1
        else:
            losses += 1
    status = "COMPLETE"
    reason = None
    if direction == MetricDirection.UNKNOWN:
        status = "NOT_COMPARABLE"
        reason = "unknown_metric_direction"
    elif baseline.status != MetricStatus.AVAILABLE or candidate.status != MetricStatus.AVAILABLE:
        status = "NOT_COMPARABLE"
        reason = "metric_not_available"
    elif not paired or wins + ties + losses == 0:
        status = "NOT_COMPARABLE"
        reason = "no_applicable_pairs"
    elif missing:
        status = "INCOMPLETE"
        reason = "missing_metric_observations"
    if status == "NOT_COMPARABLE":
        absolute = None
        relative = None
    return PairedMetric(
        baseline=baseline,
        candidate=candidate,
        absolute_delta=absolute,
        relative_delta=relative,
        paired_win=wins,
        paired_tie=ties,
        paired_loss=losses,
        applicable_pairs=wins + ties + losses,
        missing_pairs=missing,
        direction=direction,
        status=status,
        reason=reason,
    )


def metric_direction(name: str) -> MetricDirection:
    try:
        return METRIC_DEFINITIONS[name].direction
    except KeyError as exc:
        raise ValueError(f"UNKNOWN_METRIC_DEFINITION:{name}") from exc


def _binary_metric(name: str, value: bool, *, applicable: bool = True) -> MetricValue:
    if not applicable:
        return _not_applicable(name, "not_applicable")
    return MetricValue(name, MetricStatus.AVAILABLE, int(value), 1, int(value), 1)


def _rate_metric(name: str, numerator: int, denominator: int) -> MetricValue:
    if denominator == 0:
        return _not_applicable(name, "zero_denominator")
    return MetricValue(
        name, MetricStatus.AVAILABLE, numerator / denominator, 1, numerator, denominator
    )


def _recall(name: str, relevant: Sequence[str], found: Sequence[str]) -> MetricValue:
    if not relevant:
        return _not_applicable(name, "ground_truth_empty")
    numerator = len(set(relevant).intersection(found))
    return _rate_metric(name, numerator, len(set(relevant)))


def _precision(name: str, expected: Sequence[str], found: Sequence[str]) -> MetricValue:
    if not found:
        return _not_applicable(name, "no_citations")
    numerator = len(set(expected).intersection(found))
    return _rate_metric(name, numerator, len(set(found)))


def _mrr(name: str, relevant: Sequence[str], found: Sequence[str]) -> MetricValue:
    if not relevant:
        return _not_applicable(name, "ground_truth_empty")
    for index, chunk_id in enumerate(found, start=1):
        if chunk_id in relevant:
            return MetricValue(name, MetricStatus.AVAILABLE, 1 / index, 1)
    return MetricValue(name, MetricStatus.AVAILABLE, 0, 1)


def _task_success(
    values: Iterable[MetricValue], *, zero_is_success: set[str] | None = None
) -> MetricValue:
    applicable = [value for value in values if value.status == MetricStatus.AVAILABLE]
    if not applicable:
        return _not_available("task_success", "no_deterministic_observation")
    zero_is_success = zero_is_success or set()
    return _binary_metric(
        "task_success",
        all(value.value == (0 if value.name in zero_is_success else 1) for value in applicable),
    )


def _not_available(name: str, reason: str, *, version: str = "v1") -> MetricValue:
    return MetricValue(
        name, MetricStatus.NOT_AVAILABLE, None, 0, reason=reason, evaluator_version=version
    )


def _not_applicable(name: str, reason: str, *, version: str = "v1") -> MetricValue:
    return MetricValue(
        name, MetricStatus.NOT_APPLICABLE, None, 0, reason=reason, evaluator_version=version
    )


def _with_version(metric: MetricValue, version: str) -> MetricValue:
    return MetricValue(
        metric.name,
        metric.status,
        metric.value,
        metric.sample_count,
        metric.numerator,
        metric.denominator,
        metric.reason,
        version,
        metric.details,
    )


def _unique(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(dict.fromkeys(str(item) for item in value))


def _manifest_key(category: str) -> str:
    return {
        "RETRIEVAL": "retrieval-evaluator",
        "KNOWLEDGE_QA": "citation-evaluator",
        "TOOL": "tool-evaluator",
        "NO_ANSWER": "failure-evaluator",
        "APPROVAL": "approval-evaluator",
        "MULTI_STEP": "tool-evaluator",
        "FAILURE": "failure-evaluator",
    }[category]


def _float_value(metric: MetricValue) -> float | None:
    return (
        float(metric.value)
        if metric.status == MetricStatus.AVAILABLE and metric.value is not None
        else None
    )


def _json_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


__all__ = [
    "EvaluatorRegistry",
    "MetricAggregationKind",
    "MetricDefinition",
    "MetricDirection",
    "MetricStatus",
    "MetricValue",
    "PairedMetric",
    "aggregate_metric_values",
    "compare_metric_values",
    "evaluate_case",
    "evaluate_faithfulness",
    "metric_direction",
    "percentile",
]


def _definition(
    name: str,
    direction: MetricDirection,
    aggregation_kind: MetricAggregationKind,
    *,
    task_success_relevant: bool = False,
) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        direction=direction,
        aggregation_kind=aggregation_kind,
        task_success_relevant=task_success_relevant,
    )


METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    name: _definition(name, direction, kind, task_success_relevant=task_success)
    for name, direction, kind, task_success in [
        ("task_success", MetricDirection.HIGHER_IS_BETTER, MetricAggregationKind.BINARY, True),
        (
            "tool_selection_accuracy",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "tool_argument_accuracy",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "tool_sequence_accuracy",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "approval_required_accuracy",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "approval_decision_accuracy",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "answer_correctness",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "unknown_outcome_semantics_accuracy",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        (
            "candidate_recall_at_20",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.RATE,
            False,
        ),
        ("final_recall_at_5", MetricDirection.HIGHER_IS_BETTER, MetricAggregationKind.RATE, False),
        ("mrr_at_5", MetricDirection.HIGHER_IS_BETTER, MetricAggregationKind.RATE, False),
        ("citation_precision", MetricDirection.HIGHER_IS_BETTER, MetricAggregationKind.RATE, False),
        ("citation_coverage", MetricDirection.HIGHER_IS_BETTER, MetricAggregationKind.RATE, False),
        ("faithfulness", MetricDirection.HIGHER_IS_BETTER, MetricAggregationKind.BINARY, False),
        (
            "required_steps_covered",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.RATE,
            True,
        ),
        (
            "terminal_task_success",
            MetricDirection.HIGHER_IS_BETTER,
            MetricAggregationKind.BINARY,
            True,
        ),
        ("latency_p50_ms", MetricDirection.LOWER_IS_BETTER, MetricAggregationKind.SCALAR, False),
        ("latency_p95_ms", MetricDirection.LOWER_IS_BETTER, MetricAggregationKind.SCALAR, False),
        (
            "repetition_latency_mean_ms",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.SCALAR,
            False,
        ),
        (
            "repetition_latency_stddev_ms",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.SCALAR,
            False,
        ),
        (
            "unexpected_failure_rate",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.RATE,
            False,
        ),
        ("loop_rate", MetricDirection.LOWER_IS_BETTER, MetricAggregationKind.RATE, False),
        (
            "denied_action_execution_rate",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.RATE,
            True,
        ),
        (
            "unauthorized_execution_rate",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.RATE,
            True,
        ),
        (
            "duplicate_side_effect_rate",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.RATE,
            True,
        ),
        (
            "cost_per_successful_case",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.COST,
            False,
        ),
        (
            "cost_per_successful_execution",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.COST,
            False,
        ),
        (
            "cost_per_successful_dataset_item",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.COST,
            False,
        ),
        (
            "unknown_usage_count",
            MetricDirection.LOWER_IS_BETTER,
            MetricAggregationKind.SCALAR,
            False,
        ),
        ("input_tokens", MetricDirection.UNKNOWN, MetricAggregationKind.SCALAR, False),
        ("output_tokens", MetricDirection.UNKNOWN, MetricAggregationKind.SCALAR, False),
        ("total_tokens", MetricDirection.UNKNOWN, MetricAggregationKind.SCALAR, False),
        ("cached_tokens", MetricDirection.UNKNOWN, MetricAggregationKind.SCALAR, False),
    ]
}
