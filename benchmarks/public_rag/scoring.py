"""Document-level retrieval scoring for public RAG benchmarks.

Qrels are supplied as ``{question_id: {document_id: relevance_grade}}`` and a
ranking is an ordered sequence of document IDs.  The evaluator is deliberately
small and dependency-free so benchmark runners can pass ordinary Python
mappings and sequences without importing production retrieval code.

Relevant documents are deduplicated before rank is assigned.  The first
occurrence of a document wins, which prevents several chunks from the same
document from consuming multiple positions in a document-level ranking.
Questions with no qrels and questions whose qrels contain no positive grade are
reported as skipped.  A judged question with a missing or empty ranking remains
in the aggregate with zero metrics.

The paired bootstrap resamples connected groups of questions.  Two questions
belong to the same group when their positive gold document sets overlap,
directly or through another question.  Resampling groups rather than individual
questions avoids treating overlapping gold documents as independent evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, log2
from random import Random
from statistics import fmean

Qrels = Mapping[str, Mapping[str, int]]
Ranking = Sequence[str]

METRIC_NAMES = (
    "Recall@5",
    "Recall@10",
    "Recall@20",
    "MRR@5",
    "MRR@10",
    "nDCG@5",
    "nDCG@10",
    "nDCG@20",
)


@dataclass(frozen=True, slots=True)
class QuestionEvaluation:
    """Metrics and diagnostics for one question."""

    question_id: str
    gold_documents: tuple[str, ...]
    ranking: tuple[str, ...]
    grades: tuple[int, ...]
    metrics: Mapping[str, float]
    missing_documents: tuple[str, ...]
    ranking_present: bool
    skipped_unjudged: bool = False
    skipped_no_positive: bool = False

    @property
    def evaluated(self) -> bool:
        """Whether this question contributes to aggregate metrics."""

        return not self.skipped_unjudged and not self.skipped_no_positive

    @property
    def skipped(self) -> bool:
        """Whether this question is excluded from aggregate metrics."""

        return not self.evaluated

    @property
    def gold_coverage(self) -> float:
        """Fraction of positive gold documents present anywhere in the ranking."""

        if not self.gold_documents:
            return 0.0
        return (len(self.gold_documents) - len(self.missing_documents)) / len(self.gold_documents)


@dataclass(frozen=True, slots=True)
class AggregateEvaluation:
    """Per-question results and coverage counts for a complete evaluation."""

    questions: Mapping[str, QuestionEvaluation]
    metrics: Mapping[str, float]
    question_count: int
    evaluated_count: int
    skipped_unjudged_count: int
    skipped_no_positive_count: int

    @property
    def skipped_count(self) -> int:
        """Number of questions excluded from aggregate metrics."""

        return self.skipped_unjudged_count + self.skipped_no_positive_count

    @property
    def evaluation_coverage(self) -> float:
        """Share of observed questions that contribute to aggregate metrics."""

        if self.question_count == 0:
            return 0.0
        return self.evaluated_count / self.question_count

    @property
    def missing_ranking_count(self) -> int:
        """Evaluated questions for which the runner supplied no ranking entry."""

        return sum(
            not result.ranking_present for result in self.questions.values() if result.evaluated
        )

    @property
    def missing_document_count(self) -> int:
        """Number of positive gold documents absent from evaluated rankings."""

        return sum(
            len(result.missing_documents) for result in self.questions.values() if result.evaluated
        )

    @property
    def gold_coverage(self) -> float:
        """Gold documents retrieved across evaluated questions, as a fraction."""

        gold_count = sum(
            len(result.gold_documents) for result in self.questions.values() if result.evaluated
        )
        if gold_count == 0:
            return 0.0
        return (gold_count - self.missing_document_count) / gold_count


@dataclass(frozen=True, slots=True)
class PairedBootstrapResult:
    """Paired metric difference and a percentile confidence interval."""

    estimate: float
    ci_low: float
    ci_high: float
    sample_count: int
    group_count: int
    confidence: float


def evaluate_questions(
    qrels: Qrels,
    rankings: Mapping[str, Ranking],
) -> AggregateEvaluation:
    """Evaluate document rankings and return per-query and aggregate results.

    ``rankings`` may omit a judged question to represent an empty retrieval
    result.  Ranking keys that do not occur in ``qrels`` are reported as
    unjudged and skipped.  Relevance grades must be non-negative integers; only
    positive grades are retrieval targets.
    """

    results: dict[str, QuestionEvaluation] = {}
    question_ids = tuple(qrels) + tuple(
        question_id for question_id in rankings if question_id not in qrels
    )
    for question_id in question_ids:
        _validate_identifier(question_id, field="question_id")
        raw_qrels = qrels.get(question_id, {})
        graded_documents = _positive_qrels(question_id, raw_qrels)
        ranking_present = question_id in rankings
        unique_ranking = _unique_documents(rankings.get(question_id, ()), question_id=question_id)

        if not raw_qrels:
            results[question_id] = _skipped_result(
                question_id=question_id,
                ranking=unique_ranking,
                ranking_present=ranking_present,
                skipped_unjudged=True,
            )
            continue

        if not graded_documents:
            results[question_id] = _skipped_result(
                question_id=question_id,
                ranking=unique_ranking,
                ranking_present=ranking_present,
                skipped_no_positive=True,
            )
            continue

        grades = tuple(graded_documents.get(document_id, 0) for document_id in unique_ranking)
        metrics = _document_metrics(
            ranking=unique_ranking,
            graded_documents=graded_documents,
            grades=grades,
        )
        ranked_documents = set(unique_ranking)
        missing = tuple(
            document_id for document_id in graded_documents if document_id not in ranked_documents
        )
        results[question_id] = QuestionEvaluation(
            question_id=question_id,
            gold_documents=tuple(graded_documents),
            ranking=unique_ranking,
            grades=grades,
            metrics=metrics,
            missing_documents=missing,
            ranking_present=ranking_present,
        )

    evaluated = tuple(result for result in results.values() if result.evaluated)
    aggregate_metrics = {
        metric: fmean(result.metrics[metric] for result in evaluated) if evaluated else 0.0
        for metric in METRIC_NAMES
    }
    return AggregateEvaluation(
        questions=results,
        metrics=aggregate_metrics,
        question_count=len(results),
        evaluated_count=len(evaluated),
        skipped_unjudged_count=sum(result.skipped_unjudged for result in results.values()),
        skipped_no_positive_count=sum(result.skipped_no_positive for result in results.values()),
    )


def paired_bootstrap_comparison(
    baseline: AggregateEvaluation,
    candidate: AggregateEvaluation,
    *,
    metric: str,
    sample_count: int = 2_000,
    confidence: float = 0.95,
    seed: int = 0,
) -> PairedBootstrapResult:
    """Bootstrap ``candidate - baseline`` over gold-overlap question groups.

    Both evaluations must contain the same questions and the same positive gold
    documents.  The point estimate uses every evaluated question.  Each
    bootstrap sample draws connected question groups with replacement and then
    averages every question in the drawn groups, so larger groups retain their
    natural weight.  A deterministic ``random.Random(seed)`` drives resampling;
    the confidence interval uses the percentile method.
    """

    if metric not in METRIC_NAMES:
        raise ValueError(f"unknown retrieval metric: {metric}")
    if sample_count < 1:
        raise ValueError("sample_count must be at least 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if set(baseline.questions) != set(candidate.questions):
        raise ValueError("baseline and candidate must contain the same questions")

    paired_values: dict[str, tuple[float, float]] = {}
    for question_id, baseline_question in baseline.questions.items():
        candidate_question = candidate.questions[question_id]
        if baseline_question.gold_documents != candidate_question.gold_documents:
            raise ValueError(f"baseline and candidate gold documents differ for {question_id}")
        if baseline_question.evaluated != candidate_question.evaluated:
            raise ValueError(f"baseline and candidate evaluated status differs for {question_id}")
        if not baseline_question.evaluated:
            continue

        baseline_value = baseline_question.metrics[metric]
        candidate_value = candidate_question.metrics[metric]
        if not isfinite(baseline_value) or not isfinite(candidate_value):
            raise ValueError(f"metric values must be finite for {question_id}")
        paired_values[question_id] = (baseline_value, candidate_value)

    if not paired_values:
        raise ValueError("paired bootstrap requires at least one evaluated question")

    groups = _gold_overlap_groups(
        {question_id: baseline.questions[question_id] for question_id in paired_values}
    )
    estimate = fmean(
        candidate_value - baseline_value
        for baseline_value, candidate_value in paired_values.values()
    )

    rng = Random(seed)
    bootstrap_differences: list[float] = []
    for _ in range(sample_count):
        sampled_values: list[float] = []
        for _ in range(len(groups)):
            group = groups[rng.randrange(len(groups))]
            sampled_values.extend(
                paired_values[question_id][1] - paired_values[question_id][0]
                for question_id in group
            )
        bootstrap_differences.append(fmean(sampled_values))
    bootstrap_differences.sort()

    tail = (1.0 - confidence) / 2.0
    ci_low = _quantile(bootstrap_differences, tail)
    ci_high = _quantile(bootstrap_differences, 1.0 - tail)
    return PairedBootstrapResult(
        estimate=estimate,
        ci_low=ci_low,
        ci_high=ci_high,
        sample_count=sample_count,
        group_count=len(groups),
        confidence=confidence,
    )


def _positive_qrels(
    question_id: str,
    raw_qrels: Mapping[str, int],
) -> dict[str, int]:
    positive: dict[str, int] = {}
    for document_id, grade in raw_qrels.items():
        _validate_identifier(document_id, field="document_id")
        if isinstance(grade, bool) or not isinstance(grade, int):
            raise TypeError(f"relevance grade for {question_id}/{document_id} must be an integer")
        if grade < 0:
            raise ValueError(
                f"relevance grade for {question_id}/{document_id} must be non-negative"
            )
        if grade > 0:
            positive[document_id] = grade
    return positive


def _unique_documents(ranking: Ranking, *, question_id: str) -> tuple[str, ...]:
    if isinstance(ranking, (str, bytes)):
        raise TypeError(f"ranking for {question_id} must be a sequence of document IDs")

    seen: set[str] = set()
    unique: list[str] = []
    for document_id in ranking:
        _validate_identifier(document_id, field=f"ranking document_id for {question_id}")
        if document_id not in seen:
            seen.add(document_id)
            unique.append(document_id)
    return tuple(unique)


def _validate_identifier(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field} must be a non-empty string")


def _skipped_result(
    *,
    question_id: str,
    ranking: tuple[str, ...],
    ranking_present: bool,
    skipped_unjudged: bool = False,
    skipped_no_positive: bool = False,
) -> QuestionEvaluation:
    return QuestionEvaluation(
        question_id=question_id,
        gold_documents=(),
        ranking=ranking,
        grades=(),
        metrics={metric: 0.0 for metric in METRIC_NAMES},
        missing_documents=(),
        ranking_present=ranking_present,
        skipped_unjudged=skipped_unjudged,
        skipped_no_positive=skipped_no_positive,
    )


def _document_metrics(
    *,
    ranking: tuple[str, ...],
    graded_documents: Mapping[str, int],
    grades: tuple[int, ...],
) -> dict[str, float]:
    return {
        "Recall@5": _recall_at_k(ranking, graded_documents, k=5),
        "Recall@10": _recall_at_k(ranking, graded_documents, k=10),
        "Recall@20": _recall_at_k(ranking, graded_documents, k=20),
        "MRR@5": _reciprocal_rank_at_k(ranking, graded_documents, k=5),
        "MRR@10": _reciprocal_rank_at_k(ranking, graded_documents, k=10),
        "nDCG@5": _ndcg_at_k(ranking, graded_documents, grades, k=5),
        "nDCG@10": _ndcg_at_k(ranking, graded_documents, grades, k=10),
        "nDCG@20": _ndcg_at_k(ranking, graded_documents, grades, k=20),
    }


def _recall_at_k(
    ranking: tuple[str, ...],
    graded_documents: Mapping[str, int],
    *,
    k: int,
) -> float:
    retrieved = set(ranking[:k])
    return len(retrieved.intersection(graded_documents)) / len(graded_documents)


def _reciprocal_rank_at_k(
    ranking: tuple[str, ...],
    graded_documents: Mapping[str, int],
    *,
    k: int,
) -> float:
    for rank, document_id in enumerate(ranking[:k], start=1):
        if document_id in graded_documents:
            return 1.0 / rank
    return 0.0


def _ndcg_at_k(
    ranking: tuple[str, ...],
    graded_documents: Mapping[str, int],
    grades: tuple[int, ...],
    *,
    k: int,
) -> float:
    dcg = sum((2**grade - 1) / log2(rank + 1) for rank, grade in enumerate(grades[:k], start=1))
    ideal_grades = sorted(graded_documents.values(), reverse=True)[:k]
    idcg = sum((2**grade - 1) / log2(rank + 1) for rank, grade in enumerate(ideal_grades, start=1))
    return dcg / idcg


def _gold_overlap_groups(
    questions: Mapping[str, QuestionEvaluation],
) -> tuple[tuple[str, ...], ...]:
    parent = {question_id: question_id for question_id in questions}

    def find(question_id: str) -> str:
        while parent[question_id] != question_id:
            parent[question_id] = parent[parent[question_id]]
            question_id = parent[question_id]
        return question_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_question_by_document: dict[str, str] = {}
    for question_id, result in questions.items():
        for document_id in result.gold_documents:
            first_question = first_question_by_document.setdefault(document_id, question_id)
            if first_question != question_id:
                union(first_question, question_id)

    groups: dict[str, list[str]] = {}
    for question_id in questions:
        groups.setdefault(find(question_id), []).append(question_id)
    return tuple(tuple(group) for group in groups.values())


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - lower_index
    return sorted_values[lower_index] + fraction * (
        sorted_values[upper_index] - sorted_values[lower_index]
    )


__all__ = [
    "AggregateEvaluation",
    "METRIC_NAMES",
    "PairedBootstrapResult",
    "QuestionEvaluation",
    "evaluate_questions",
    "paired_bootstrap_comparison",
]
