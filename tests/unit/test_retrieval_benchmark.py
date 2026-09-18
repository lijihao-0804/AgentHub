from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.retrieval.metrics import (
    BenchmarkHit,
    BenchmarkRetrieval,
    classify_failure,
    evaluate_dataset,
    locator_matches,
    reciprocal_rank_at_k,
    relevant_set_recall,
)
from benchmarks.retrieval.schema import (
    BenchmarkCase,
    GroundTruth,
    dataset_from_payload,
    load_dataset,
)


def _ground_truth(*section_keys: str) -> tuple[GroundTruth, ...]:
    return tuple(
        GroundTruth(
            document_key="doc-a",
            revision_key="doc-a-r1",
            locator={"type": "section", "section_key": section_key},
            relevant_text=section_key,
        )
        for section_key in section_keys
    )


def _hit(section_key: str, *, score: float = 1.0) -> BenchmarkHit:
    return BenchmarkHit(
        document_key="doc-a",
        revision_key="doc-a-r1",
        locator={"type": "section", "section_key": section_key},
        score=score,
        chunk_id=section_key,
    )


def test_locator_matching_uses_text_range_overlap() -> None:
    assert locator_matches(
        {"type": "text_range", "char_start": 10, "char_end": 30},
        {"type": "text_range", "char_start": 25, "char_end": 45},
    )
    assert not locator_matches(
        {"type": "text_range", "char_start": 10, "char_end": 30},
        {"type": "text_range", "char_start": 30, "char_end": 45},
    )


def test_metrics_support_multiple_relevant_items_and_first_rank() -> None:
    ground_truth = _ground_truth("one", "two")
    candidates = (_hit("one"), _hit("noise"))
    final = (_hit("two"), _hit("noise"))

    assert relevant_set_recall(candidates, ground_truth) == 0.5
    assert relevant_set_recall(final, ground_truth) == 0.5
    assert reciprocal_rank_at_k(final, ground_truth, k=5) == 1.0


def test_failure_categories_are_stage_specific() -> None:
    ground_truth = _ground_truth("target")
    assert classify_failure((_hit("noise"),), (_hit("noise"),), ground_truth) == "FIRST_STAGE_MISS"
    assert classify_failure((_hit("target"),), (_hit("noise"),), ground_truth) == "RERANK_DROP"
    assert classify_failure(
        (_hit("target"),), (_hit("noise"), _hit("target")), ground_truth
    ) == "FINAL_RANK_LOW"
    assert classify_failure((_hit("target"),), (_hit("target"),), ground_truth) is None


def test_evaluate_dataset_wires_deterministic_fake_retrieval() -> None:
    case = BenchmarkCase(
        case_id="fake-1",
        split="dev",
        query="find target",
        ground_truth=_ground_truth("target"),
    )
    dataset = dataset_from_payload(
        {
            "dataset_version": "fake",
            "corpus": [
                {
                    "document_key": f"doc-{index}",
                    "revision_key": f"doc-{index}-r1",
                    "title": f"Doc {index}",
                    "sections": [{"section_key": "section", "text": "content"}],
                }
                for index in range(8)
            ],
            "cases": [
                {
                    "id": case.case_id,
                    "split": case.split,
                    "query": case.query,
                    "ground_truth": [
                        {
                            "document_key": "doc-0",
                            "revision_key": "doc-0-r1",
                            "locator": {"type": "section", "section_key": "section"},
                            "relevant_text": "content",
                        }
                    ],
                },
                {
                    "id": "fake-2",
                    "split": "holdout",
                    "query": "holdout query",
                    "ground_truth": [
                        {
                            "document_key": "doc-1",
                            "revision_key": "doc-1-r1",
                            "locator": {"type": "section", "section_key": "section"},
                            "relevant_text": "content",
                        }
                    ],
                },
            ],
        },
        expected_case_count=None,
    )
    result = evaluate_dataset(
        dataset,
        lambda current: BenchmarkRetrieval(
            candidate_hits=(
                BenchmarkHit(
                    document_key=current.ground_truth[0].document_key,
                    revision_key=current.ground_truth[0].revision_key,
                    locator=current.ground_truth[0].locator,
                    score=1.0,
                ),
            ),
            final_hits=(),
        ),
    )

    assert result.metrics("dev")["candidate_recall_at_20"] == 1.0
    assert result.metrics("dev")["final_recall_at_5"] == 0.0
    assert result.failures("dev")[0]["failure_category"] == "RERANK_DROP"


def test_real_dataset_has_stable_hash_and_fixed_split() -> None:
    path = Path("benchmarks/retrieval/dataset.json")
    first = load_dataset(path)
    second = load_dataset(path)

    assert first.dataset_hash == second.dataset_hash
    assert len(first.corpus) == 10
    assert len(first.cases) == 30
    assert sum(case.split == "dev" for case in first.cases) == 20
    assert sum(case.split == "holdout" for case in first.cases) == 10


def test_dataset_validator_rejects_duplicate_queries() -> None:
    payload = {
        "dataset_version": "invalid",
        "corpus": [
            {
                "document_key": f"doc-{index}",
                "revision_key": f"doc-{index}-r1",
                "title": f"Doc {index}",
                "sections": [{"section_key": "section", "text": "content"}],
            }
            for index in range(8)
        ],
        "cases": [
            {
                "id": f"case-{index}",
                "split": "dev" if index == 0 else "holdout",
                "query": "same query",
                "ground_truth": [
                    {
                        "document_key": "doc-0",
                        "revision_key": "doc-0-r1",
                        "locator": {"type": "section", "section_key": "section"},
                        "relevant_text": "content",
                    }
                ],
            }
            for index in range(2)
        ],
    }

    with pytest.raises(ValueError, match="duplicate query"):
        dataset_from_payload(payload, expected_case_count=None)
