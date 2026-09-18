"""Lightweight, reproducible retrieval evaluation baseline."""

from benchmarks.retrieval.metrics import (
    BenchmarkHit,
    BenchmarkRetrieval,
    EvaluationResult,
    evaluate_dataset,
)
from benchmarks.retrieval.schema import (
    BenchmarkCase,
    CorpusDocument,
    GroundTruth,
    RetrievalDataset,
    load_dataset,
    validate_dataset,
)

__all__ = [
    "BenchmarkCase",
    "BenchmarkHit",
    "BenchmarkRetrieval",
    "CorpusDocument",
    "EvaluationResult",
    "GroundTruth",
    "RetrievalDataset",
    "evaluate_dataset",
    "load_dataset",
    "validate_dataset",
]
