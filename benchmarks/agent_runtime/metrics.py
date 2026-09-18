"""Metrics and safe case comparison for the M4 runtime baseline."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from benchmarks.agent_runtime.schema import AgentEvaluationCase, AgentEvaluationDataset

FAILURE_CATEGORIES = (
    "STATUS_MISMATCH",
    "FAILURE_CODE_MISMATCH",
    "TOOL_SEQUENCE_MISMATCH",
    "MODEL_STEP_MISMATCH",
    "TOOL_CALL_COUNT_MISMATCH",
    "FINAL_OUTPUT_MISMATCH",
    "UNEXPECTED_HANDLER_EXECUTION",
    "RUNNER_ERROR",
)


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    status: str
    failure_code: str | None
    tool_sequence: tuple[str, ...]
    model_steps: int
    tool_calls: int
    final_output: str | None
    handler_calls: int
    duration_ms: float


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    split: str
    category: str
    status: str
    failure_code: str | None
    expected_status: str
    expected_failure_code: str | None
    expected_tool_sequence: tuple[str, ...]
    actual_tool_sequence: tuple[str, ...]
    expected_model_steps: int
    actual_model_steps: int
    expected_tool_calls: int
    actual_tool_calls: int
    final_output_match: bool
    expected_handler_calls: int
    actual_handler_calls: int
    case_pass: bool
    duration_ms: float
    failure_category: str | None
    failure_details: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "split": self.split,
            "category": self.category,
            "status": self.status,
            "failure_code": self.failure_code,
            "expected_status": self.expected_status,
            "expected_failure_code": self.expected_failure_code,
            "expected_tool_sequence": list(self.expected_tool_sequence),
            "actual_tool_sequence": list(self.actual_tool_sequence),
            "expected_model_steps": self.expected_model_steps,
            "actual_model_steps": self.actual_model_steps,
            "expected_tool_calls": self.expected_tool_calls,
            "actual_tool_calls": self.actual_tool_calls,
            "final_output_match": self.final_output_match,
            "expected_handler_calls": self.expected_handler_calls,
            "actual_handler_calls": self.actual_handler_calls,
            "case_pass": self.case_pass,
            "duration_ms": self.duration_ms,
            "failure_category": self.failure_category,
            "failure_details": list(self.failure_details),
        }


@dataclass(frozen=True, slots=True)
class AgentEvaluationResult:
    dataset_version: str
    dataset_hash: str
    git_commit: str
    cases: tuple[CaseEvaluation, ...]

    def metrics(self, split: str | None = None) -> dict[str, float]:
        selected = [case for case in self.cases if split is None or case.split == split]
        if not selected:
            return {
                "case_pass_rate": 0.0,
                "tool_sequence_accuracy": 0.0,
                "terminal_status_accuracy": 0.0,
                "failure_code_accuracy": 0.0,
            }
        return {
            "case_pass_rate": _rate(case.case_pass for case in selected),
            "tool_sequence_accuracy": _rate(
                case.actual_tool_sequence == case.expected_tool_sequence for case in selected
            ),
            "terminal_status_accuracy": _rate(
                case.status == case.expected_status for case in selected
            ),
            "failure_code_accuracy": _rate(
                case.failure_code == case.expected_failure_code for case in selected
            ),
        }

    def category_metrics(self, split: str | None = None) -> dict[str, float]:
        categories = sorted({case.category for case in self.cases})
        return {
            category: _rate(
                case.case_pass
                for case in self.cases
                if case.category == category and (split is None or case.split == split)
            )
            for category in categories
        }

    def failure_analysis(self) -> list[dict[str, Any]]:
        return [
            {
                "case_id": case.case_id,
                "split": case.split,
                "category": case.category,
                "failure_category": case.failure_category,
                "details": list(case.failure_details),
            }
            for case in self.cases
            if not case.case_pass
        ]

    def to_payload(self) -> dict[str, Any]:
        return {
            "benchmark": "m4-agent-runtime-evaluation",
            "dataset_version": self.dataset_version,
            "dataset_hash": self.dataset_hash,
            "git_commit": self.git_commit,
            "case_count": len(self.cases),
            "split_counts": dict(Counter(case.split for case in self.cases)),
            "category_counts": dict(Counter(case.category for case in self.cases)),
            "metrics": {
                "dev": self.metrics("dev"),
                "holdout": self.metrics("holdout"),
                "overall": self.metrics(),
            },
            "category_pass_rate": {
                "dev": self.category_metrics("dev"),
                "holdout": self.category_metrics("holdout"),
                "overall": self.category_metrics(),
            },
            "failure_analysis": self.failure_analysis(),
            "case_results": [case.to_payload() for case in self.cases],
        }


def evaluate_case(case: AgentEvaluationCase, observation: RuntimeObservation) -> CaseEvaluation:
    expected = case.expected
    checks = (
        ("status", observation.status == expected.status),
        ("failure_code", observation.failure_code == expected.failure_code),
        ("tool_sequence", observation.tool_sequence == expected.tool_sequence),
        ("model_steps", observation.model_steps == expected.model_steps),
        ("tool_calls", observation.tool_calls == expected.tool_calls),
        (
            "final_output",
            observation.final_output == expected.final_output,
        ),
        ("handler_calls", observation.handler_calls == expected.handler_calls),
    )
    details = tuple(name for name, passed in checks if not passed)
    failure_category = _failure_category(details)
    return CaseEvaluation(
        case_id=case.case_id,
        split=case.split,
        category=case.category,
        status=observation.status,
        failure_code=observation.failure_code,
        expected_status=expected.status,
        expected_failure_code=expected.failure_code,
        expected_tool_sequence=expected.tool_sequence,
        actual_tool_sequence=observation.tool_sequence,
        expected_model_steps=expected.model_steps,
        actual_model_steps=observation.model_steps,
        expected_tool_calls=expected.tool_calls,
        actual_tool_calls=observation.tool_calls,
        final_output_match=observation.final_output == expected.final_output,
        expected_handler_calls=expected.handler_calls,
        actual_handler_calls=observation.handler_calls,
        case_pass=not details,
        duration_ms=observation.duration_ms,
        failure_category=failure_category,
        failure_details=details,
    )


def evaluate_results(
    dataset: AgentEvaluationDataset,
    observations: dict[str, RuntimeObservation],
    *,
    git_commit: str,
) -> AgentEvaluationResult:
    cases: list[CaseEvaluation] = []
    for case in dataset.cases:
        observation = observations.get(case.case_id)
        if observation is None:
            observation = RuntimeObservation(
                status="FAILED",
                failure_code="RUNNER_ERROR",
                tool_sequence=(),
                model_steps=0,
                tool_calls=0,
                final_output=None,
                handler_calls=0,
                duration_ms=0.0,
            )
        cases.append(evaluate_case(case, observation))
    return AgentEvaluationResult(
        dataset_version=dataset.dataset_version,
        dataset_hash=dataset.dataset_hash,
        git_commit=git_commit,
        cases=tuple(cases),
    )


def _rate(values: Any) -> float:
    values = tuple(values)
    return round(sum(bool(value) for value in values) / len(values), 4) if values else 0.0


def _failure_category(details: tuple[str, ...]) -> str | None:
    mapping = {
        "status": "STATUS_MISMATCH",
        "failure_code": "FAILURE_CODE_MISMATCH",
        "tool_sequence": "TOOL_SEQUENCE_MISMATCH",
        "model_steps": "MODEL_STEP_MISMATCH",
        "tool_calls": "TOOL_CALL_COUNT_MISMATCH",
        "final_output": "FINAL_OUTPUT_MISMATCH",
        "handler_calls": "UNEXPECTED_HANDLER_EXECUTION",
    }
    for detail in details:
        if detail in mapping:
            return mapping[detail]
    return None


__all__ = [
    "AgentEvaluationResult",
    "CaseEvaluation",
    "FAILURE_CATEGORIES",
    "RuntimeObservation",
    "evaluate_case",
    "evaluate_results",
]
