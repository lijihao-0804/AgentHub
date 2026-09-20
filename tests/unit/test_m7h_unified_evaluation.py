from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.evaluation.dataset_builder import build_items
from benchmarks.retrieval.corpus import chunk_ids_by_section
from benchmarks.retrieval.schema import load_dataset
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


def test_unified_corpus_cases_use_formal_chunk_identity_and_no_placeholders() -> None:
    items = build_items()
    retrieval_dataset = load_dataset(Path("benchmarks/retrieval/dataset.json"))
    formal_chunk_ids = {
        chunk_id
        for document in retrieval_dataset.corpus
        for chunk_ids in chunk_ids_by_section(document).values()
        for chunk_id in chunk_ids
    }
    for item in items:
        if item["category"] == "RETRIEVAL":
            assert set(item["expected"]["relevant_chunk_ids"]).issubset(formal_chunk_ids)
        if item["category"] == "KNOWLEDGE_QA":
            assert set(item["expected"]["citations"]).issubset(formal_chunk_ids)
            assert any(
                item["expected"]["answer"] in section.text
                for document in retrieval_dataset.corpus
                for section in document.sections
            )

    serialized = json.dumps(items, ensure_ascii=False)
    assert "curated-chunk-" not in serialized
    assert "lookup" not in serialized
    assert "placeholder" not in serialized.lower()

    corpus_text = "\n".join(
        section.text.lower()
        for document in retrieval_dataset.corpus
        for section in document.sections
    )
    absent_markers = {
        "curated-no-answer-01": "carryover limit",
        "curated-no-answer-02": "payroll date",
        "curated-no-answer-03": "remote-work days",
        "curated-no-answer-04": "rotate a company service passphrase",
        "curated-no-answer-05": "settle an approved refund",
        "curated-no-answer-06": "guaranteed resolution deadline",
        "curated-no-answer-07": "insurance certificate expiry date",
        "curated-no-answer-08": "maximum attachment size",
        "curated-no-answer-09": "emergency data access",
        "curated-no-answer-10": "tax percentage",
    }
    for item in items:
        if item["category"] == "NO_ANSWER":
            assert item["expected"]["answerable"] is False
            assert absent_markers[item["case_key"]] not in corpus_text

    allowed_tools = {"query_customer", "search_knowledge", "calculator"}
    for item in items:
        if item["category"] == "TOOL":
            assert item["expected"]["tool_identity"] in allowed_tools
        if item["category"] == "MULTI_STEP":
            assert set(item["expected"]["steps"]).issubset(allowed_tools)


def test_historical_semantics_and_splits_are_not_reassigned() -> None:
    items = build_items()
    by_key = {item["case_key"]: item for item in items}
    m3 = json.loads(Path("benchmarks/retrieval/dataset.json").read_text(encoding="utf-8"))
    for case in m3["cases"]:
        assert by_key[f"m3-{case['id']}"]["split"] == case["split"].upper()

    m4 = json.loads(Path("benchmarks/agent_runtime/dataset.json").read_text(encoding="utf-8"))
    for case in m4["cases"]:
        if case["category"] == "approval_unavailable":
            item = by_key[f"m4-{case['case_id']}"]
            assert item["split"] == case["split"].upper()
            assert item["expected"]["decision"] != "PENDING"
            assert item["expected"]["failure_code"] == case["expected"]["failure_code"]

    m5 = json.loads(Path("benchmarks/approval_runtime/dataset.json").read_text(encoding="utf-8"))
    for case in m5["cases"][:7]:
        item = by_key[f"m5-{case['case_id']}"]
        assert item["split"] == case["split"].upper()
        assert item["expected"]["execution_status"] == case["expected"]["execution_status"]
        assert item["expected"]["run_status"] == case["expected"]["run_status"]

    m6 = json.loads(Path("benchmarks/observability/dataset.json").read_text(encoding="utf-8"))
    assert all("split" not in case for case in m6["cases"])
    assert all(by_key[f"m6-{case['case_id']}"]["split"] == "DEV" for case in m6["cases"])

    for item in items:
        if item["category"] == "NO_ANSWER":
            assert item["expected"]["answerable"] is False
