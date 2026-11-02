"""Rule-based failure taxonomy for agent evaluation (AE-5).

Automatic root-cause attribution is still weak (TRAIL's best model scores 11% on error
localization), so this module deliberately does only the cheap, auditable part: map
observable trajectory facts to a small fixed code set, and point at the first offending
step.  Anything that needs judgment is left to a human label.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TAXONOMY_VERSION = "failure-taxonomy-v1"

FAILURE_CODES: dict[str, str] = {
    "RUN_NOT_SUCCEEDED": "运行终态不是 SUCCEEDED",
    "RUN_FAILED": "运行失败",
    "BUDGET_EXHAUSTED": "预算/轮数/调用数耗尽",
    "REQUIRED_TOOL_MISSING": "缺少必需工具调用",
    "FORBIDDEN_TOOL_CALL": "调用了禁止工具",
    "TOOL_ORDER_VIOLATION": "工具顺序违反约束",
    "EXCESSIVE_TOOL_CALLS": "重复/冗余工具调用",
    "AGENT_LOOP": "触发相同调用循环保护",
    "APPROVAL_BYPASS": "审批拒绝后仍执行",
    "APPROVAL_DECISION_MISMATCH": "审批决定与预期不符",
    "ANSWER_MISMATCH": "终态答案不匹配",
    "NO_TOOL_CALLS": "需要工具但一次也没有调用",
}


def classify_failure(
    expected: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    task_success: bool | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic failure labels for one case result.

    Labels never guess: a code is emitted only when the corresponding fact is present.
    ``source`` is ``RULE`` and ``confidence`` is 1.0 because each label is a direct
    reading of a recorded fact, not a model judgment.
    """

    if observation.get("trajectory_version") is None:
        return []

    labels: list[dict[str, Any]] = []
    step_index = observation.get("first_error_step_index")
    calls = observation.get("tool_calls")
    calls = [call for call in calls if isinstance(call, Mapping)] if isinstance(calls, list) else []
    identities = [str(call.get("identity")) for call in calls if call.get("identity")]

    required = _expected_tools(expected)
    forbidden = set(_string_list(expected.get("forbidden_tools")))
    precedence = expected.get("precedence")

    if required and not identities:
        labels.append(_label("NO_TOOL_CALLS", step_index))
    missing = [identity for identity in required if identity not in identities]
    if required and missing:
        labels.append(_label("REQUIRED_TOOL_MISSING", step_index, {"missing": missing}))
    hit_forbidden = [identity for identity in identities if identity in forbidden]
    if hit_forbidden:
        labels.append(_label("FORBIDDEN_TOOL_CALL", step_index, {"tools": hit_forbidden}))
    violation = _first_order_violation(precedence, identities)
    if violation is not None:
        labels.append(_label("TOOL_ORDER_VIOLATION", step_index, {"pair": list(violation)}))

    facts = observation.get("approval_facts")
    if isinstance(facts, list):
        for fact in facts:
            if not isinstance(fact, Mapping):
                continue
            decision = str(fact.get("decision_status", "")).upper()
            execution = str(fact.get("execution_status", "")).upper()
            if decision == "DENIED" and execution == "SUCCEEDED":
                labels.append(_label("APPROVAL_BYPASS", step_index))
            expected_decision = expected.get("decision")
            if expected_decision and decision and decision != str(expected_decision).upper():
                labels.append(_label("APPROVAL_DECISION_MISMATCH", step_index))

    if observation.get("loop_guard_triggered"):
        labels.append(_label("AGENT_LOOP", step_index))
    signatures = observation.get("tool_call_signatures")
    if isinstance(signatures, list) and len(signatures) > len(set(map(str, signatures))):
        labels.append(_label("EXCESSIVE_TOOL_CALLS", step_index))

    status = str(observation.get("terminal_status") or "")
    if status and status != "SUCCEEDED":
        if "BUDGET" in status or "MAX_TOOL" in status:
            labels.append(_label("BUDGET_EXHAUSTED", step_index))
        elif status in {"FAILED", "CANCELLED"}:
            labels.append(_label("RUN_FAILED", step_index))
        else:
            labels.append(_label("RUN_NOT_SUCCEEDED", step_index))

    if task_success is False and not labels:
        labels.append(_label("ANSWER_MISMATCH", step_index))
    return labels


def summarize_labels(labels: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        code = str(label.get("code"))
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def _label(
    code: str, step_index: Any, detail: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "code": code,
        "description": FAILURE_CODES.get(code, code),
        "step_index": step_index,
        "source": "RULE",
        "confidence": 1.0,
        "detail": dict(detail or {}),
        "taxonomy_version": TAXONOMY_VERSION,
    }


def _expected_tools(expected: Mapping[str, Any]) -> list[str]:
    for key in ("required_tools", "tool_sequence"):
        value = expected.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return _string_list(value)
    identity = expected.get("tool_identity")
    return [identity] if isinstance(identity, str) and identity else []


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def _first_order_violation(
    precedence: Any, identities: Sequence[str]
) -> tuple[str, str] | None:
    if not isinstance(precedence, Sequence) or isinstance(precedence, (str, bytes)):
        return None
    positions: dict[str, int] = {}
    for index, identity in enumerate(identities):
        positions.setdefault(identity, index)
    for pair in precedence:
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
            continue
        before, after = str(pair[0]), str(pair[1])
        if (
            before not in positions
            or after not in positions
            or positions[before] > positions[after]
        ):
            return (before, after)
    return None


__all__ = [
    "FAILURE_CODES",
    "TAXONOMY_VERSION",
    "classify_failure",
    "summarize_labels",
]
