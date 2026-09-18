from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash

DATASET_VERSION = "m5-approval-runtime-v1"
EXPECTED_CASE_COUNT = 20
EXPECTED_SPLIT_COUNTS = {"dev": 14, "holdout": 6}
EXPECTED_CATEGORIES = frozenset(
    {
        "approval_required",
        "approval_denied",
        "approval_approved",
        "duplicate_approve",
        "multi_step_action",
        "self_approval_denied",
        "restart_resume",
        "idempotent_action",
        "unknown_outcome",
        "cancel_race",
        "crash_recovery",
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalCase:
    case_id: str
    split: str
    category: str
    actions: tuple[str, ...]
    expected: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalDataset:
    dataset_version: str
    cases: tuple[ApprovalCase, ...]
    dataset_hash: str


def load_dataset(path: Path) -> ApprovalDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"dataset_version", "cases"}:
        raise ValueError("dataset must contain dataset_version and cases")
    version = payload["dataset_version"]
    raw_cases = payload["cases"]
    if version != DATASET_VERSION or not isinstance(raw_cases, list):
        raise ValueError("invalid M5 dataset envelope")
    cases: list[ApprovalCase] = []
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise ValueError(f"cases[{index}] must be an object")
        if set(raw) != {"case_id", "split", "category", "actions", "expected"}:
            raise ValueError(f"cases[{index}] has unsupported fields")
        if (
            not isinstance(raw["case_id"], str)
            or not isinstance(raw["split"], str)
            or not isinstance(raw["category"], str)
            or not isinstance(raw["actions"], list)
            or not all(isinstance(action, str) for action in raw["actions"])
            or not isinstance(raw["expected"], dict)
        ):
            raise ValueError(f"cases[{index}] has invalid fields")
        cases.append(
            ApprovalCase(
                case_id=raw["case_id"],
                split=raw["split"],
                category=raw["category"],
                actions=tuple(raw["actions"]),
                expected=dict(raw["expected"]),
            )
        )
    dataset = ApprovalDataset(version, tuple(cases), canonical_json_hash(payload))
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: ApprovalDataset) -> None:
    if dataset.dataset_version != DATASET_VERSION:
        raise ValueError(f"dataset_version must be {DATASET_VERSION}")
    if len(dataset.cases) != EXPECTED_CASE_COUNT:
        raise ValueError(f"dataset must contain exactly {EXPECTED_CASE_COUNT} cases")
    ids = [case.case_id for case in dataset.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case_id values must be unique")
    splits = {
        split: sum(case.split == split for case in dataset.cases)
        for split in EXPECTED_SPLIT_COUNTS
    }
    if splits != EXPECTED_SPLIT_COUNTS:
        raise ValueError(f"split counts must be {EXPECTED_SPLIT_COUNTS}")
    if not all(case.category in EXPECTED_CATEGORIES for case in dataset.cases):
        raise ValueError("dataset contains an unsupported category")
    if {case.category for case in dataset.cases} != EXPECTED_CATEGORIES:
        raise ValueError("dataset must cover every required M5 category")


def dataset_summary(dataset: ApprovalDataset) -> dict[str, Any]:
    return {
        "dataset_version": dataset.dataset_version,
        "dataset_hash": dataset.dataset_hash,
        "case_count": len(dataset.cases),
        "split_counts": {
            split: sum(case.split == split for case in dataset.cases)
            for split in EXPECTED_SPLIT_COUNTS
        },
        "category_counts": {
            category: sum(case.category == category for case in dataset.cases)
            for category in sorted(EXPECTED_CATEGORIES)
        },
    }


__all__ = [
    "ApprovalCase",
    "ApprovalDataset",
    "DATASET_VERSION",
    "EXPECTED_CASE_COUNT",
    "EXPECTED_CATEGORIES",
    "EXPECTED_SPLIT_COUNTS",
    "dataset_summary",
    "load_dataset",
    "validate_dataset",
]
