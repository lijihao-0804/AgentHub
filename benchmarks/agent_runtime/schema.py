"""Strict dataset schema and validation for the M4 runtime baseline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash

DATASET_VERSION = "m4-agent-runtime-v1"
EXPECTED_CASE_COUNT = 20
EXPECTED_SPLIT_COUNTS = {"dev": 14, "holdout": 6}
EXPECTED_CATEGORY_COUNTS = {
    "tool_selection": 6,
    "no_tool": 4,
    "multi_step_read": 4,
    "loop_guard": 3,
    "approval_unavailable": 3,
}
_SPLITS = frozenset(EXPECTED_SPLIT_COUNTS)
_CATEGORIES = frozenset(EXPECTED_CATEGORY_COUNTS)
_STATUSES = frozenset({"SUCCEEDED", "FAILED"})
_SCRIPT_TYPES = frozenset({"FINAL", "TOOL_CALL", "MULTI_TOOL_CALL"})


@dataclass(frozen=True, slots=True)
class ScriptStep:
    type: str
    content: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    calls: tuple[tuple[str, dict[str, Any]], ...] = ()


@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    status: str
    failure_code: str | None
    tool_sequence: tuple[str, ...]
    model_steps: int
    tool_calls: int
    final_output: str | None
    handler_calls: int


@dataclass(frozen=True, slots=True)
class AgentEvaluationCase:
    case_id: str
    split: str
    category: str
    input: str
    model_script: tuple[ScriptStep, ...]
    expected: ExpectedOutcome


@dataclass(frozen=True, slots=True)
class AgentEvaluationDataset:
    dataset_version: str
    cases: tuple[AgentEvaluationCase, ...]
    dataset_hash: str


def _object(value: Any, *, field: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    extra = set(value) - keys
    if extra:
        raise ValueError(f"{field} has unsupported fields: {sorted(extra)}")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _parse_call(value: Any, *, field: str, allow_type: bool = False) -> tuple[str, dict[str, Any]]:
    keys = {"name", "arguments"}
    if allow_type:
        keys.add("type")
    raw = _object(value, field=field, keys=keys)
    name = _text(raw.get("name"), field=f"{field}.name")
    arguments = raw.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError(f"{field}.arguments must be an object")
    return name, dict(arguments)


def _parse_script(value: Any, *, field: str) -> ScriptStep:
    raw = _object(
        value,
        field=field,
        keys={"type", "content", "name", "arguments", "calls"},
    )
    step_type = _text(raw.get("type"), field=f"{field}.type")
    if step_type not in _SCRIPT_TYPES:
        raise ValueError(f"{field}.type is unsupported")
    if step_type == "FINAL":
        if set(raw) != {"type", "content"}:
            raise ValueError(f"{field} FINAL requires only type and content")
        return ScriptStep(
            type=step_type, content=_text(raw.get("content"), field=f"{field}.content")
        )
    if step_type == "TOOL_CALL":
        if set(raw) != {"type", "name", "arguments"}:
            raise ValueError(f"{field} TOOL_CALL requires type, name and arguments")
        name, arguments = _parse_call(raw, field=field, allow_type=True)
        return ScriptStep(type=step_type, name=name, arguments=arguments)
    if set(raw) != {"type", "calls"}:
        raise ValueError(f"{field} MULTI_TOOL_CALL requires only type and calls")
    calls = raw.get("calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError(f"{field}.calls must be a non-empty list")
    return ScriptStep(
        type=step_type,
        calls=tuple(
            _parse_call(call, field=f"{field}.calls[{index}]") for index, call in enumerate(calls)
        ),
    )


def _parse_expected(value: Any, *, field: str) -> ExpectedOutcome:
    raw = _object(
        value,
        field=field,
        keys={
            "status",
            "failure_code",
            "tool_sequence",
            "model_steps",
            "tool_calls",
            "final_output",
            "handler_calls",
        },
    )
    status = _text(raw.get("status"), field=f"{field}.status")
    if status not in _STATUSES:
        raise ValueError(f"{field}.status is unsupported")
    failure_code = raw.get("failure_code")
    if failure_code is not None:
        failure_code = _text(failure_code, field=f"{field}.failure_code")
    sequence = raw.get("tool_sequence")
    if not isinstance(sequence, list) or any(not isinstance(item, str) for item in sequence):
        raise ValueError(f"{field}.tool_sequence must be a list of strings")
    final_output = raw.get("final_output")
    if final_output is not None and not isinstance(final_output, str):
        raise ValueError(f"{field}.final_output must be a string or null")
    return ExpectedOutcome(
        status=status,
        failure_code=failure_code,
        tool_sequence=tuple(sequence),
        model_steps=_non_negative_int(raw.get("model_steps"), field=f"{field}.model_steps"),
        tool_calls=_non_negative_int(raw.get("tool_calls"), field=f"{field}.tool_calls"),
        final_output=final_output,
        handler_calls=_non_negative_int(raw.get("handler_calls"), field=f"{field}.handler_calls"),
    )


def _parse_case(value: Any, index: int) -> AgentEvaluationCase:
    field = f"cases[{index}]"
    raw = _object(
        value,
        field=field,
        keys={"case_id", "split", "category", "input", "model_script", "expected"},
    )
    script = raw.get("model_script")
    if not isinstance(script, list) or not script:
        raise ValueError(f"{field}.model_script must be a non-empty list")
    return AgentEvaluationCase(
        case_id=_text(raw.get("case_id"), field=f"{field}.case_id"),
        split=_text(raw.get("split"), field=f"{field}.split"),
        category=_text(raw.get("category"), field=f"{field}.category"),
        input=_text(raw.get("input"), field=f"{field}.input"),
        model_script=tuple(
            _parse_script(item, field=f"{field}.model_script[{script_index}]")
            for script_index, item in enumerate(script)
        ),
        expected=_parse_expected(raw.get("expected"), field=f"{field}.expected"),
    )


def dataset_from_payload(payload: Any) -> AgentEvaluationDataset:
    raw = _object(payload, field="dataset", keys={"dataset_version", "cases"})
    version = _text(raw.get("dataset_version"), field="dataset_version")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise ValueError("cases must be a list")
    cases = tuple(_parse_case(item, index) for index, item in enumerate(cases_raw))
    dataset = AgentEvaluationDataset(
        dataset_version=version,
        cases=cases,
        dataset_hash=canonical_json_hash(payload),
    )
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: AgentEvaluationDataset) -> None:
    if dataset.dataset_version != DATASET_VERSION:
        raise ValueError(f"dataset_version must be {DATASET_VERSION}")
    if len(dataset.cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"dataset must contain exactly {EXPECTED_CASE_COUNT} cases")
    case_ids: set[str] = set()
    split_counts = {split: 0 for split in _SPLITS}
    category_counts = {category: 0 for category in _CATEGORIES}
    category_split_counts: dict[tuple[str, str], int] = {}
    for case in dataset.cases:
        if case.case_id in case_ids:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        case_ids.add(case.case_id)
        if case.split not in _SPLITS:
            raise ValueError(f"unsupported split: {case.split}")
        if case.category not in _CATEGORIES:
            raise ValueError(f"unsupported category: {case.category}")
        split_counts[case.split] += 1
        category_counts[case.category] += 1
        key = (case.category, case.split)
        category_split_counts[key] = category_split_counts.get(key, 0) + 1
    if split_counts != EXPECTED_SPLIT_COUNTS:
        raise ValueError(f"split counts must be {EXPECTED_SPLIT_COUNTS}")
    if category_counts != EXPECTED_CATEGORY_COUNTS:
        raise ValueError(f"category counts must be {EXPECTED_CATEGORY_COUNTS}")
    if any(
        category_split_counts.get((category, split), 0) == 0
        for category in _CATEGORIES
        for split in _SPLITS
    ):
        raise ValueError("every category must appear in both splits")


def load_dataset(path: Path) -> AgentEvaluationDataset:
    return dataset_from_payload(json.loads(path.read_text(encoding="utf-8")))


def dataset_summary(dataset: AgentEvaluationDataset) -> dict[str, Any]:
    return {
        "dataset_version": dataset.dataset_version,
        "dataset_hash": dataset.dataset_hash,
        "case_count": len(dataset.cases),
        "split_counts": {
            split: sum(case.split == split for case in dataset.cases) for split in _SPLITS
        },
        "category_counts": {
            category: sum(case.category == category for case in dataset.cases)
            for category in _CATEGORIES
        },
    }


__all__ = [
    "AgentEvaluationCase",
    "AgentEvaluationDataset",
    "DATASET_VERSION",
    "EXPECTED_CATEGORY_COUNTS",
    "EXPECTED_CASE_COUNT",
    "EXPECTED_SPLIT_COUNTS",
    "ExpectedOutcome",
    "ScriptStep",
    "dataset_from_payload",
    "dataset_summary",
    "load_dataset",
    "validate_dataset",
]
