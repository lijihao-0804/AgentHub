"""Trajectory evidence for agent evaluation (AE-1).

The evaluation platform already persists ``run_steps`` and ``approvals``.  This module
turns those execution facts into a *content-free* projection (``trajectory-v1``) and
scores it.  It never reads tool argument text, tool output text or the final answer:
arguments are represented by canonical hashes, and the projection records where every
field came from so an auditor can go back to the event stream.

Two rules shape the design:

* **Evidence, not labels.**  A trajectory is built from what the runtime recorded.  A
  field the runtime did not record becomes ``NOT_AVAILABLE``; it is never guessed and
  never silently scored as zero.
* **Compatibility without reinterpretation.**  The projection also supplies the keys the
  pre-existing evaluators already read (``tool_identity``, ``tool_sequence``,
  ``terminal_status`` ...), so an experiment frozen with the new manifest can score the
  same categories the old evaluators defined.  The new trajectory metrics are additive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.metrics import MetricStatus, MetricValue

TRAJECTORY_VERSION = "trajectory-v1"
TRAJECTORY_SCHEMA_VERSION = 3
TRAJECTORY_EVALUATOR_VERSION = "trajectory-v1"

#: Metrics produced only from a trajectory projection.  Their evaluator version is the
#: trajectory version, so a change in projection semantics changes the version and the
#: comparison guard refuses to pair runs scored under different semantics.
TRAJECTORY_METRIC_NAMES: tuple[str, ...] = (
    "tool_precision",
    "tool_recall",
    "tool_f1",
    "forbidden_tool_call_rate",
    "tool_order_violation_rate",
    "redundant_call_rate",
    "recovery_rate",
    "approval_respected",
    "tool_call_efficiency",
    "trajectory_success",
)

_LOOP_GUARD_CODES = frozenset({"AGENT_IDENTICAL_TOOL_CALL_LIMIT"})
_FAILED_STATUSES = frozenset({"FAILED", "CANCELLED"})


def build_trajectory_observation(
    *,
    steps: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]] = (),
    terminal: Mapping[str, Any] | None = None,
    counters: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    output_hash: str | None = None,
    category: str | None = None,
    expected: Mapping[str, Any] | None = None,
    field_sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the ``trajectory-v1`` projection from persisted execution facts.

    ``steps`` items are expected to carry ``index``, ``kind``, ``status``,
    ``tool_identities``, ``arguments_hashes`` and optional ``error_code``.  The function
    is pure: it does not query the database and does not mutate its inputs.
    """

    normalized_steps = [_normalize_step(index, step) for index, step in enumerate(steps)]
    tool_calls = _tool_calls(normalized_steps)
    tool_sequence = [call["identity"] for call in tool_calls]
    signatures = [_call_signature(call) for call in tool_calls]
    approval_facts = [_normalize_approval(item) for item in approvals]
    loop_guard = any(
        step["kind"] == "GUARD" and step.get("error_code") in _LOOP_GUARD_CODES
        for step in normalized_steps
    )
    first_error = next(
        (
            step["index"]
            for step in normalized_steps
            if step.get("error_code") or step.get("status") in _FAILED_STATUSES
        ),
        None,
    )

    projection: dict[str, Any] = {
        "observation_schema_version": TRAJECTORY_SCHEMA_VERSION,
        "trajectory_version": TRAJECTORY_VERSION,
        "trajectory_step_count": len(normalized_steps),
        "trajectory_steps": normalized_steps,
        "tool_calls": tool_calls,
        "tool_sequence": tool_sequence,
        "tool_call_signatures": signatures,
        "loop_guard_triggered": loop_guard,
        "first_error_step_index": first_error,
        "counters": dict(counters or {}),
        "budget": dict(budget or {}),
        "redaction": {
            "rules_version": "trajectory-redaction-v1",
            "dropped": ["arguments_raw", "tool_output", "final_output_text", "private_facts"],
        },
        "field_sources": dict(field_sources or {}),
    }

    if tool_calls:
        projection["tool_identity"] = tool_calls[0]["identity"]
        projection["steps"] = tool_sequence
        first_hash = tool_calls[0].get("arguments_hash")
        if isinstance(first_hash, str) and first_hash:
            projection["arguments_hash"] = first_hash

    if terminal is not None:
        projection["terminal_status"] = terminal.get("status")
        projection["observed_agent_status"] = terminal.get("status")
        projection["observed_agent_failure_code"] = terminal.get("failure_code")

    if approval_facts:
        last = approval_facts[-1]
        projection["approval_required"] = True
        projection["approval_decision"] = last.get("decision_status")
        projection["action_executed"] = last.get("execution_status") == "SUCCEEDED"
        projection["approval_facts"] = approval_facts
    else:
        projection["approval_required"] = False

    if category == "KNOWLEDGE_QA" and output_hash:
        # Exact-answer cases compare canonical hashes; the projection never exposes the
        # answer text.  Open-ended cases still need the judge and stay NOT_APPLICABLE.
        projection["answer_hash"] = output_hash

    if expected is not None:
        projection["expected_tool_count"] = len(_expected_tools(expected))

    return projection


def evaluate_trajectory(
    expected: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, MetricValue]:
    """Score a ``trajectory-v1`` observation; return an empty mapping otherwise."""

    if observation.get("trajectory_version") != TRAJECTORY_VERSION:
        return {}

    calls = observation.get("tool_calls")
    calls = [call for call in calls if isinstance(call, Mapping)] if isinstance(calls, list) else []
    identities = [
        str(call.get("identity")) for call in calls if isinstance(call.get("identity"), str)
    ]
    required = _expected_tools(expected)
    required_set = set(required)
    forbidden = set(_string_list(expected.get("forbidden_tools")))
    signatures = [
        str(item) for item in observation.get("tool_call_signatures") or [] if isinstance(item, str)
    ]

    metrics: dict[str, MetricValue] = {}

    metrics["tool_precision"] = _precision("tool_precision", identities, required_set)
    metrics["tool_recall"] = _recall("tool_recall", identities, required)
    precision = metrics["tool_precision"]
    recall = metrics["tool_recall"]
    if _available(precision) and _available(recall):
        p = float(precision.value)
        r = float(recall.value)
        f1 = 0.0 if p + r == 0 else 2 * p * r / (p + r)
        metrics["tool_f1"] = _rate("tool_f1", f1, 1, 1)
    else:
        metrics["tool_f1"] = _not_applicable("tool_f1", "precision_or_recall_unavailable")

    metrics["forbidden_tool_call_rate"] = _binary_rate(
        "forbidden_tool_call_rate",
        any(identity in forbidden for identity in identities),
        applicable=bool(forbidden),
        reason="no_forbidden_tools_declared",
    )
    metrics["tool_order_violation_rate"] = _order_violation(expected, identities)
    metrics["redundant_call_rate"] = _redundant_rate(identities, signatures)
    metrics["recovery_rate"] = _recovery_rate(observation.get("trajectory_steps"))
    metrics["approval_respected"] = _approval_respected(observation.get("approval_facts"))
    metrics["tool_call_efficiency"] = _efficiency(required, identities)

    if "loop_guard_triggered" in observation:
        metrics["loop_rate"] = _binary_rate(
            "loop_rate", bool(observation.get("loop_guard_triggered")), applicable=True
        )
    else:
        metrics["loop_rate"] = _not_available("loop_rate", "loop_guard_not_observed")

    metrics["trajectory_success"] = _trajectory_success(observation, metrics)
    return metrics


def _normalize_step(index: int, step: Mapping[str, Any]) -> dict[str, Any]:
    identities = _string_list(step.get("tool_identities"))
    hashes = _string_list(step.get("arguments_hashes"))
    return {
        "index": int(step.get("index", index)),
        "sequence_number": step.get("sequence_number"),
        "kind": str(step.get("kind") or "UNKNOWN"),
        "status": str(step.get("status") or "UNKNOWN"),
        "error_code": step.get("error_code"),
        "tool_identities": identities,
        "arguments_hashes": hashes,
    }


def _normalize_approval(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": str(item.get("approval_id")) if item.get("approval_id") else None,
        "logical_action_id": item.get("logical_action_id"),
        "tool_identity": item.get("tool_identity"),
        "arguments_hash": item.get("arguments_hash"),
        "decision_status": str(item.get("decision_status") or "PENDING"),
        "execution_status": str(item.get("execution_status") or "NOT_STARTED"),
    }


def _tool_calls(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for step in steps:
        if step["kind"] != "TOOL_PROPOSAL":
            continue
        identities = step["tool_identities"]
        hashes = step["arguments_hashes"]
        for index, identity in enumerate(identities):
            calls.append(
                {
                    "identity": identity,
                    "arguments_hash": hashes[index] if index < len(hashes) else None,
                }
            )
    if calls:
        return calls
    # Older runs may have an execution step without a matching proposal projection.
    for step in steps:
        if step["kind"] != "TOOL_EXECUTE":
            continue
        for identity in step["tool_identities"]:
            calls.append({"identity": identity, "arguments_hash": None})
    return calls


def _call_signature(call: Mapping[str, Any]) -> str:
    payload: dict[str, Any] = {"tool": call.get("identity")}
    if call.get("arguments_hash"):
        payload["arguments_hash"] = call["arguments_hash"]
    return canonical_json_hash(payload)


def _expected_tools(expected: Mapping[str, Any]) -> list[str]:
    for key in ("required_tools", "tool_sequence"):
        value = expected.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return _string_list(value)
    identity = expected.get("tool_identity")
    return [str(identity)] if isinstance(identity, str) and identity else []


def _precision(
    name: str, identities: Sequence[str], required: set[str]
) -> MetricValue:
    if not required:
        return _not_applicable(name, "no_required_tools_declared")
    if not identities:
        return _not_applicable(name, "no_tool_calls")
    numerator = sum(1 for identity in identities if identity in required)
    return _rate(name, numerator, len(identities), 1)


def _recall(name: str, identities: Sequence[str], required: Sequence[str]) -> MetricValue:
    if not required:
        return _not_applicable(name, "no_required_tools_declared")
    found = set(identities)
    numerator = sum(1 for identity in set(required) if identity in found)
    return _rate(name, numerator, len(set(required)), 1)


def _order_violation(expected: Mapping[str, Any], identities: Sequence[str]) -> MetricValue:
    pairs = expected.get("precedence")
    if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)) or not pairs:
        return _not_applicable("tool_order_violation_rate", "no_precedence_declared")
    positions: dict[str, int] = {}
    for index, identity in enumerate(identities):
        positions.setdefault(identity, index)
    for pair in pairs:
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
            return _not_available("tool_order_violation_rate", "precedence_invalid")
        before, after = str(pair[0]), str(pair[1])
        if before not in positions or after not in positions:
            return _binary_rate("tool_order_violation_rate", True, applicable=True)
        if positions[before] > positions[after]:
            return _binary_rate("tool_order_violation_rate", True, applicable=True)
    return _binary_rate("tool_order_violation_rate", False, applicable=True)


def _redundant_rate(identities: Sequence[str], signatures: Sequence[str]) -> MetricValue:
    if not identities:
        return _not_applicable("redundant_call_rate", "no_tool_calls")
    unique = len(set(signatures)) if signatures else len(set(identities))
    return _rate(
        "redundant_call_rate", max(0, len(identities) - unique), len(identities), 1
    )


def _recovery_rate(steps: Any) -> MetricValue:
    if not isinstance(steps, list):
        return _not_available("recovery_rate", "trajectory_steps_missing")
    failures = recovered = 0
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or step.get("kind") != "TOOL_EXECUTE":
            continue
        failed = step.get("status") in _FAILED_STATUSES or bool(step.get("error_code"))
        if not failed:
            continue
        identities = set(_string_list(step.get("tool_identities")))
        if not identities:
            continue
        failures += 1
        for later in steps[index + 1 :]:
            if (
                isinstance(later, Mapping)
                and later.get("kind") == "TOOL_EXECUTE"
                and later.get("status") == "SUCCEEDED"
                and identities.intersection(_string_list(later.get("tool_identities")))
            ):
                recovered += 1
                break
    if failures == 0:
        return _not_applicable("recovery_rate", "no_tool_failure_observed")
    return _rate("recovery_rate", recovered, failures, failures)


def _approval_respected(facts: Any) -> MetricValue:
    if not isinstance(facts, list) or not facts:
        return _not_applicable("approval_respected", "no_approval_observed")
    violated = any(
        isinstance(fact, Mapping)
        and str(fact.get("decision_status")).upper() == "DENIED"
        and str(fact.get("execution_status")).upper() == "SUCCEEDED"
        for fact in facts
    )
    return _binary_rate("approval_respected", not violated, applicable=True)


def _efficiency(required: Sequence[str], identities: Sequence[str]) -> MetricValue:
    if not required:
        return _not_applicable("tool_call_efficiency", "no_required_tools_declared")
    if not identities:
        return _rate("tool_call_efficiency", 0, 1, 1)
    value = min(1.0, len(set(required)) / len(identities))
    return _rate("tool_call_efficiency", value, 1, 1)


def _trajectory_success(
    observation: Mapping[str, Any], metrics: Mapping[str, MetricValue]
) -> MetricValue:
    status = observation.get("terminal_status")
    if not isinstance(status, str):
        return _not_available("trajectory_success", "terminal_status_missing")

    checks: list[bool] = [status == "SUCCEEDED"]
    recall = metrics.get("tool_recall")
    if recall is not None and recall.status == MetricStatus.AVAILABLE:
        # Declared required tools are a contract: missing one fails the trajectory even
        # when the terminal answer happens to be right.
        checks.append(float(recall.value) == 1)
    for name, zero_is_success in (
        ("forbidden_tool_call_rate", True),
        ("tool_order_violation_rate", True),
        ("approval_respected", False),
        ("loop_rate", True),
    ):
        metric = metrics.get(name)
        if metric is None or metric.status != MetricStatus.AVAILABLE:
            continue
        checks.append(
            float(metric.value) == 0 if zero_is_success else float(metric.value) == 1
        )
    return _binary_rate("trajectory_success", all(checks), applicable=True)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int))]


def _binary_rate(
    name: str, value: bool, *, applicable: bool, reason: str | None = None
) -> MetricValue:
    if not applicable:
        return _not_applicable(name, reason or "not_applicable")
    return _rate(name, int(bool(value)), 1, 1)


def _rate(name: str, numerator: float, denominator: float, sample_count: int) -> MetricValue:
    value = numerator / denominator if denominator else 0.0
    return MetricValue(
        name,
        MetricStatus.AVAILABLE,
        value,
        sample_count,
        numerator,
        denominator,
        evaluator_version=TRAJECTORY_EVALUATOR_VERSION,
    )


def _available(metric: MetricValue) -> bool:
    return metric.status == MetricStatus.AVAILABLE and metric.value is not None


def _not_applicable(name: str, reason: str) -> MetricValue:
    return MetricValue(
        name,
        MetricStatus.NOT_APPLICABLE,
        None,
        0,
        reason=reason,
        evaluator_version=TRAJECTORY_EVALUATOR_VERSION,
    )


def _not_available(name: str, reason: str) -> MetricValue:
    return MetricValue(
        name,
        MetricStatus.NOT_AVAILABLE,
        None,
        0,
        reason=reason,
        evaluator_version=TRAJECTORY_EVALUATOR_VERSION,
    )


__all__ = [
    "TRAJECTORY_EVALUATOR_VERSION",
    "TRAJECTORY_METRIC_NAMES",
    "TRAJECTORY_SCHEMA_VERSION",
    "TRAJECTORY_VERSION",
    "build_trajectory_observation",
    "evaluate_trajectory",
]
