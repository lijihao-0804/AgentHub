from __future__ import annotations

from types import SimpleNamespace

import pytest

from benchmarks.evaluation.dataset_builder import build_items
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.metrics import MetricStatus, evaluate_case
from packages.evaluation.runner import DeterministicEvaluationDriver
from packages.evaluation.validation import validate_dataset_items


def test_repeated_expected_identity_is_allowed_when_cases_are_distinct() -> None:
    items = [
        {
            "case_key": "approval-a",
            "split": "DEV",
            "category": "APPROVAL",
            "input": {"action": "Approve action A"},
            "expected": {"decision": "APPROVED"},
            "tags": ["fixture"],
            "source_provenance": {"source_kind": "fixture", "source_id": "approval-a"},
            "ordinal": 0,
        },
        {
            "case_key": "approval-b",
            "split": "HOLDOUT",
            "category": "APPROVAL",
            "input": {"action": "Approve action B"},
            "expected": {"decision": "APPROVED"},
            "tags": ["fixture"],
            "source_provenance": {"source_kind": "fixture", "source_id": "approval-b"},
            "ordinal": 1,
        },
    ]

    assert len(validate_dataset_items(items)) == 2


def test_duplicate_input_and_provenance_remain_rejected() -> None:
    base = {
        "case_key": "case-a",
        "split": "DEV",
        "category": "NO_ANSWER",
        "input": {"question": "same question"},
        "expected": {"answer": "No answer."},
        "tags": [],
        "source_provenance": {"source_kind": "fixture", "source_id": "a"},
        "ordinal": 0,
    }
    duplicate_input = {
        **base,
        "case_key": "case-b",
        "ordinal": 1,
        "source_provenance": {"source_kind": "fixture", "source_id": "b"},
    }
    with pytest.raises(AgentHubError, match="normalized input"):
        validate_dataset_items([base, duplicate_input])

    duplicate_provenance = {
        **base,
        "case_key": "case-c",
        "ordinal": 1,
        "input": {"question": "different question"},
    }
    with pytest.raises(AgentHubError, match="provenance"):
        validate_dataset_items([base, duplicate_provenance])


@pytest.mark.asyncio
async def test_deterministic_driver_supports_all_formal_categories() -> None:
    items = build_items()
    first_by_category = {}
    for item in items:
        first_by_category.setdefault(item["category"], item)
    driver = DeterministicEvaluationDriver()
    for category, item in first_by_category.items():
        observation = await driver.execute(
            run=SimpleNamespace(),
            variant=SimpleNamespace(variant_hash="variant-hash"),
            item=SimpleNamespace(
                case_key=item["case_key"],
                category=category,
                input=item["input"],
                expected=item["expected"],
            ),
        )
        metrics = evaluate_case(category, item["expected"], observation.observation)
        assert metrics["task_success"].status == MetricStatus.AVAILABLE, category
        assert metrics["task_success"].value == 1, category


def test_unified_dataset_has_frozen_distribution_and_all_splits() -> None:
    items = build_items()
    assert len(items) == 100
    assert {item["split"] for item in items} == {"DEV", "HOLDOUT"}
    categories = {item["category"] for item in items}
    assert categories == {
        "RETRIEVAL",
        "KNOWLEDGE_QA",
        "TOOL",
        "NO_ANSWER",
        "APPROVAL",
        "MULTI_STEP",
        "FAILURE",
    }
