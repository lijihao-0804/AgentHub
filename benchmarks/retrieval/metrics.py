"""Small, explicit retrieval metrics and failure categorization."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from benchmarks.retrieval.schema import BenchmarkCase, GroundTruth, RetrievalDataset


@dataclass(frozen=True, slots=True)
class BenchmarkHit:
    document_key: str
    revision_key: str
    locator: dict[str, Any]
    score: float
    chunk_id: str = ""


@dataclass(frozen=True, slots=True)
class BenchmarkRetrieval:
    candidate_hits: tuple[BenchmarkHit, ...]
    final_hits: tuple[BenchmarkHit, ...]


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    split: str
    query: str
    ground_truth: tuple[GroundTruth, ...]
    candidate_recall_at_20: float
    final_recall_at_5: float
    mrr_at_5: float
    failure_category: str | None
    candidate_hits: tuple[BenchmarkHit, ...]
    final_hits: tuple[BenchmarkHit, ...]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    cases: tuple[CaseEvaluation, ...]

    def metrics(self, split: str | None = None) -> dict[str, float]:
        selected = [case for case in self.cases if split is None or case.split == split]
        if not selected:
            return {
                "candidate_recall_at_20": 0.0,
                "final_recall_at_5": 0.0,
                "mrr_at_5": 0.0,
            }
        return {
            "candidate_recall_at_20": _mean(
                case.candidate_recall_at_20 for case in selected
            ),
            "final_recall_at_5": _mean(case.final_recall_at_5 for case in selected),
            "mrr_at_5": _mean(case.mrr_at_5 for case in selected),
        }

    def failures(self, split: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "case_id": case.case_id,
                "split": case.split,
                "query": case.query,
                "ground_truth": _serialize_ground_truth(case.ground_truth),
                "failure_category": case.failure_category,
                "candidate_top20": _serialize_hits(case.candidate_hits[:20]),
                "final_top5": _serialize_hits(case.final_hits[:5]),
            }
            for case in self.cases
            if case.failure_category is not None and (split is None or case.split == split)
        ]


def _mean(values: Sequence[float] | Any) -> float:
    values = tuple(values)
    return round(sum(values) / len(values), 6) if values else 0.0


def locator_matches(ground_truth: dict[str, Any], retrieved: dict[str, Any]) -> bool:
    ground_truth_type = ground_truth.get("type")
    retrieved_type = retrieved.get("type")
    if ground_truth_type != retrieved_type:
        return False
    if ground_truth_type == "section":
        return ground_truth.get("section_key") == retrieved.get("section_key")
    if ground_truth_type == "page":
        return ground_truth.get("page") == retrieved.get("page")
    if ground_truth_type == "text_range":
        return max(ground_truth["char_start"], retrieved.get("char_start", -1)) < min(
            ground_truth["char_end"], retrieved.get("char_end", -1)
        )
    return False


def hit_matches_ground_truth(hit: BenchmarkHit, ground_truth: GroundTruth) -> bool:
    return (
        hit.document_key == ground_truth.document_key
        and hit.revision_key == ground_truth.revision_key
        and locator_matches(ground_truth.locator, hit.locator)
    )


def _matched_indices(hits: Sequence[BenchmarkHit], ground_truth: Sequence[GroundTruth]) -> set[int]:
    return {
        index
        for index, expected in enumerate(ground_truth)
        if any(hit_matches_ground_truth(hit, expected) for hit in hits)
    }


def relevant_set_recall(
    hits: Sequence[BenchmarkHit], ground_truth: Sequence[GroundTruth]
) -> float:
    if not ground_truth:
        return 0.0
    return len(_matched_indices(hits, ground_truth)) / len(ground_truth)


def reciprocal_rank_at_k(
    hits: Sequence[BenchmarkHit], ground_truth: Sequence[GroundTruth], *, k: int
) -> float:
    for rank, hit in enumerate(hits[:k], start=1):
        if any(hit_matches_ground_truth(hit, expected) for expected in ground_truth):
            return 1.0 / rank
    return 0.0


def classify_failure(
    candidate_hits: Sequence[BenchmarkHit],
    final_hits: Sequence[BenchmarkHit],
    ground_truth: Sequence[GroundTruth],
) -> str | None:
    if not _matched_indices(candidate_hits[:20], ground_truth):
        return "FIRST_STAGE_MISS"
    if not _matched_indices(final_hits[:5], ground_truth):
        return "RERANK_DROP"
    first_rank = next(
        rank
        for rank, hit in enumerate(final_hits[:5], start=1)
        if any(hit_matches_ground_truth(hit, expected) for expected in ground_truth)
    )
    return "FINAL_RANK_LOW" if first_rank > 1 else None


def evaluate_case(case: BenchmarkCase, retrieval: BenchmarkRetrieval) -> CaseEvaluation:
    candidate_hits = tuple(retrieval.candidate_hits[:20])
    final_hits = tuple(retrieval.final_hits[:5])
    return CaseEvaluation(
        case_id=case.case_id,
        split=case.split,
        query=case.query,
        ground_truth=case.ground_truth,
        candidate_recall_at_20=relevant_set_recall(candidate_hits, case.ground_truth),
        final_recall_at_5=relevant_set_recall(final_hits, case.ground_truth),
        mrr_at_5=reciprocal_rank_at_k(final_hits, case.ground_truth, k=5),
        failure_category=classify_failure(candidate_hits, final_hits, case.ground_truth),
        candidate_hits=candidate_hits,
        final_hits=final_hits,
    )


def evaluate_dataset(
    dataset: RetrievalDataset,
    retrieve: Callable[[BenchmarkCase], BenchmarkRetrieval],
) -> EvaluationResult:
    return EvaluationResult(
        cases=tuple(evaluate_case(case, retrieve(case)) for case in dataset.cases)
    )


def _serialize_hits(hits: Sequence[BenchmarkHit]) -> list[dict[str, Any]]:
    return [
        {
            "document_key": hit.document_key,
            "revision_key": hit.revision_key,
            "locator": hit.locator,
            "score": round(hit.score, 6),
            "chunk_id": hit.chunk_id,
            "rank": rank,
        }
        for rank, hit in enumerate(hits, start=1)
    ]


def _serialize_ground_truth(ground_truth: Sequence[GroundTruth]) -> list[dict[str, Any]]:
    return [
        {
            "document_key": expected.document_key,
            "revision_key": expected.revision_key,
            "locator": expected.locator,
            "relevant_text": expected.relevant_text,
        }
        for expected in ground_truth
    ]


__all__ = [
    "BenchmarkHit",
    "BenchmarkRetrieval",
    "CaseEvaluation",
    "EvaluationResult",
    "classify_failure",
    "evaluate_case",
    "evaluate_dataset",
    "hit_matches_ground_truth",
    "locator_matches",
    "reciprocal_rank_at_k",
    "relevant_set_recall",
]
