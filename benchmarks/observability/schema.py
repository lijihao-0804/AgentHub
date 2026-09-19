"""Validation for the M6 controlled failure-scenario manifest."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash

DATASET_VERSION = "m6-observability-failure-v1"
EXPECTED_CASE_COUNT = 10
_CATEGORIES = frozenset({"MODEL", "KNOWLEDGE", "TOOL", "APPROVAL", "ACTION", "RUNTIME"})
_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION"})


@dataclass(frozen=True, slots=True)
class FailureScenario:
    case_id: str
    scenario: str
    expected_run_status: str
    expected_failure_category: str
    expected_failure_code: str | None
    runtime_path: str


@dataclass(frozen=True, slots=True)
class FailureScenarioDataset:
    dataset_version: str
    cases: tuple[FailureScenario, ...]
    dataset_hash: str


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def load_dataset(path: Path) -> FailureScenarioDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"dataset_version", "cases"}:
        raise ValueError("dataset must contain dataset_version and cases")
    if payload["dataset_version"] != DATASET_VERSION or not isinstance(payload["cases"], list):
        raise ValueError("invalid M6 failure dataset envelope")
    cases: list[FailureScenario] = []
    for index, raw in enumerate(payload["cases"]):
        if not isinstance(raw, dict):
            raise ValueError(f"cases[{index}] must be an object")
        expected = {
            "case_id",
            "scenario",
            "expected_run_status",
            "expected_failure_category",
            "expected_failure_code",
            "runtime_path",
        }
        if set(raw) != expected:
            raise ValueError(f"cases[{index}] has unsupported fields")
        failure_code = raw["expected_failure_code"]
        if failure_code is not None:
            failure_code = _text(failure_code, f"cases[{index}].expected_failure_code")
        cases.append(
            FailureScenario(
                case_id=_text(raw["case_id"], f"cases[{index}].case_id"),
                scenario=_text(raw["scenario"], f"cases[{index}].scenario"),
                expected_run_status=_text(
                    raw["expected_run_status"], f"cases[{index}].expected_run_status"
                ),
                expected_failure_category=_text(
                    raw["expected_failure_category"],
                    f"cases[{index}].expected_failure_category",
                ),
                expected_failure_code=failure_code,
                runtime_path=_text(raw["runtime_path"], f"cases[{index}].runtime_path"),
            )
        )
    dataset = FailureScenarioDataset(
        dataset_version=payload["dataset_version"],
        cases=tuple(cases),
        dataset_hash=canonical_json_hash(payload),
    )
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: FailureScenarioDataset) -> None:
    if dataset.dataset_version != DATASET_VERSION:
        raise ValueError(f"dataset_version must be {DATASET_VERSION}")
    if len(dataset.cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"dataset must contain exactly {EXPECTED_CASE_COUNT} cases")
    ids = [case.case_id for case in dataset.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case_id values must be unique")
    if any(case.expected_run_status not in _STATUSES for case in dataset.cases):
        raise ValueError("dataset contains an unsupported run status")
    if any(case.expected_failure_category not in _CATEGORIES for case in dataset.cases):
        raise ValueError("dataset contains an unsupported failure category")
    if any(not case.runtime_path.startswith("AgentRunService") for case in dataset.cases):
        raise ValueError("every case must identify the real AgentRunService path")


def dataset_summary(dataset: FailureScenarioDataset) -> dict[str, Any]:
    return {
        "dataset_version": dataset.dataset_version,
        "dataset_hash": dataset.dataset_hash,
        "case_count": len(dataset.cases),
        "categories": sorted({case.expected_failure_category for case in dataset.cases}),
    }


__all__ = [
    "DATASET_VERSION",
    "EXPECTED_CASE_COUNT",
    "FailureScenario",
    "FailureScenarioDataset",
    "dataset_summary",
    "load_dataset",
    "validate_dataset",
]
