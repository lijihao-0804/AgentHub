"""Score recorded production traces without re-running retrieval or model calls."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.scifact_runner import DATA, dump
from benchmarks.public_rag.scoring import evaluate_questions, paired_bootstrap_comparison

STAGES = ("dense", "sparse", "fusion", "rerank")
OPTIONAL_IDENTITY_KEYS = (
    "chunk_size_chars",
    "chunk_overlap_chars",
    "knowledge_chunk_size_chars",
    "knowledge_chunk_overlap_chars",
    "text_policy",
    "embedding_batch_size",
    "knowledge_embedding_batch_size",
    "reranker_batch_size",
    "knowledge_reranker_batch_size",
    "dense_vector_size",
    "knowledge_dense_vector_size",
    "embedding_model_revision",
    "reranker_model_revision",
    "sparse",
    "sparse_modifier",
    "collection",
    "database",
    "torch",
    "device",
    "git_commit",
)


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def read_run(path: Path) -> tuple[dict, dict]:
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        query_id = row["query_id"]
        if query_id in rows:
            raise ValueError(f"duplicate query ID in {path.name}: {query_id}")
        rows[query_id] = row
    if len(rows) != meta["queries"]:
        raise ValueError(f"incomplete run: {len(rows)}/{meta['queries']}")
    return meta, rows


def rankings(rows: dict, stage: str, chunk_budget: int = 20) -> dict[str, tuple[str, ...]]:
    # DENSE would cut to candidate_top_k before final_top_k in production.
    # Its raw trace has 30 hits; comparing all 30 against 20 fused chunks
    # would silently give the dense projection a larger candidate budget.
    return {
        query_id: tuple(
            hit["document_id"] for hit in row["stages"][stage]["ranking"][:chunk_budget]
        )
        for query_id, row in rows.items()
    }


def _projected_diagnostics(
    rows: dict,
    *,
    evaluated_query_ids: set[str],
    chunk_budget: int = 20,
) -> dict[str, dict[str, object]]:
    diagnostics = {}
    for stage in STAGES:
        total_chunks = 0
        unique_documents = 0
        duplicate_chunks = 0
        empty = []
        missing = []
        for query_id, row in rows.items():
            projected = row["stages"][stage]["ranking"][:chunk_budget]
            total_chunks += len(projected)
            documents = {hit["document_id"] for hit in projected}
            unique_documents += len(documents)
            duplicate_chunks += len(projected) - len(documents)
            if query_id not in evaluated_query_ids:
                continue
            if not projected:
                empty.append(query_id)
            if len(documents) < chunk_budget:
                missing.append(query_id)
        diagnostics[stage] = {
            "projected_chunks": total_chunks,
            "projected_unique_documents": unique_documents,
            "duplicate_chunks": duplicate_chunks,
            "duplicate_chunk_rate": duplicate_chunks / total_chunks if total_chunks else 0.0,
            "empty_ranking_count": len(empty),
            "empty_ranking_queries": empty,
            "candidate_shortfall_count": len(missing),
            "candidate_shortfall_queries": missing,
        }
    return diagnostics


def _stage_transition_failures(evaluations: dict) -> dict[str, list[str]]:
    failures = {
        "dense_miss_at_20": [],
        "sparse_miss_at_20": [],
        "fusion_miss_at_20": [],
        "rerank_miss_at_20": [],
        "dense_to_fusion_loss": [],
        "rerank_loss_at_5": [],
    }
    for query_id, dense_question in evaluations["dense"].questions.items():
        if not dense_question.evaluated:
            continue
        metrics = {stage: evaluations[stage].questions[query_id].metrics for stage in STAGES}
        for stage in STAGES:
            if metrics[stage]["Recall@20"] < 1:
                failures[f"{stage}_miss_at_20"].append(query_id)
        if metrics["fusion"]["Recall@20"] < metrics["dense"]["Recall@20"]:
            failures["dense_to_fusion_loss"].append(query_id)
        if metrics["rerank"]["Recall@5"] < metrics["fusion"]["Recall@5"]:
            failures["rerank_loss_at_5"].append(query_id)
    return failures


def _validate_paired_identity(baseline_meta: dict, candidate_meta: dict) -> None:
    required_identity_keys = (
        "split",
        "queries",
        "seed",
        "snapshot_id",
        "snapshot_hash",
        "embedding_model",
        "reranker_model",
        "dense_top_k",
        "sparse_top_k",
        "candidate_top_k",
        "final_top_k",
    )
    for key in required_identity_keys:
        if baseline_meta[key] != candidate_meta[key]:
            raise ValueError(f"unpaired experiment identity: {key}")
    for key in OPTIONAL_IDENTITY_KEYS:
        if key in baseline_meta and key in candidate_meta:
            if baseline_meta[key] != candidate_meta[key]:
                raise ValueError(f"unpaired experiment identity: {key}")
    if (
        "retrieval_identity" in baseline_meta
        and "retrieval_identity" in candidate_meta
        and baseline_meta["retrieval_identity"] != candidate_meta["retrieval_identity"]
    ):
        raise ValueError("unpaired experiment identity: retrieval_identity")
    if any("paired_interleaved" in meta for meta in (baseline_meta, candidate_meta)):
        if not all(
            meta.get("paired_interleaved") is True for meta in (baseline_meta, candidate_meta)
        ):
            raise ValueError("paired runs must both use paired_interleaved=True")


def _record_test_scoring_access(path: Path, meta: dict) -> None:
    if meta.get("split") != "test":
        return
    with path.parent.joinpath("test-exposures.jsonl").open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {
                    "event": "TEST_SCORING_LABEL_ACCESS",
                    "at": datetime.now(UTC).isoformat(),
                    "run": path.name,
                }
            )
            + "\n"
        )


def evaluate(path: Path, data: Path) -> tuple[dict, dict]:
    meta, rows = read_run(path)
    _record_test_scoring_access(path, meta)
    source = load_scifact(data, meta["split"])
    if set(rows) - set(source.qrels):
        raise ValueError("run contains queries outside declared official split")
    # A complete run must cover the actual split, not just its own self-reported size.
    if len(rows) != len(source.qrels):
        raise ValueError("primary report requires the complete labeled split")
    evaluations = {
        stage: evaluate_questions(source.qrels, rankings(rows, stage)) for stage in STAGES
    }
    evaluated_query_ids = {
        query_id
        for query_id, question in evaluations["dense"].questions.items()
        if question.evaluated
    }
    diagnostics = _projected_diagnostics(rows, evaluated_query_ids=evaluated_query_ids)
    stages = {}
    for stage, evaluation in evaluations.items():
        evaluated_questions = {
            query_id: question
            for query_id, question in evaluation.questions.items()
            if question.evaluated
        }
        stages[stage] = {
            "metrics": dict(evaluation.metrics),
            "evaluated_count": evaluation.evaluated_count,
            "skipped_unjudged": evaluation.skipped_unjudged_count,
            "skipped_no_positive": evaluation.skipped_no_positive_count,
            "missing_ranking_count": evaluation.missing_ranking_count,
            "empty_ranking_count": sum(not q.ranking for q in evaluated_questions.values()),
            "empty_ranking_queries": diagnostics[stage]["empty_ranking_queries"],
            "candidate_duplicate_chunks": diagnostics[stage]["duplicate_chunks"],
            "candidate_duplicate_rate": diagnostics[stage]["duplicate_chunk_rate"],
            "candidate_shortfall_count": diagnostics[stage]["candidate_shortfall_count"],
            "candidate_shortfall_queries": diagnostics[stage]["candidate_shortfall_queries"],
            "missing_gold_documents": evaluation.missing_document_count,
            "mean_unique_documents": (
                fmean(len(q.ranking) for q in evaluated_questions.values())
                if evaluated_questions
                else 0.0
            ),
            "questions": {qid: asdict(q) for qid, q in evaluation.questions.items()},
        }
    warm_rows = [row for row in rows.values() if not row["first_inference"]]
    latency = {}
    for name in ("total", "dense", "sparse", "fusion", "rerank"):
        values = [
            row["total_ms"] if name == "total" else row["stages"][name]["latency_ms"]
            for row in warm_rows
        ]
        latency[name] = {
            "p50_ms": quantile(values, 0.5),
            "p95_ms": quantile(values, 0.95),
            "mean_ms": fmean(values) if values else None,
            "n": len(values),
        }
    first = next((row for row in rows.values() if row["first_inference"]), None)
    failures = _stage_transition_failures(evaluations)
    for stage in STAGES:
        stages[stage]["miss_at_20_count"] = len(failures[f"{stage}_miss_at_20"])
        stages[stage]["miss_at_20_queries"] = failures[f"{stage}_miss_at_20"]
    return {
        "metadata": meta,
        "ranking_projection": "first 20 chunks per stage, then document dedup before metric rank",
        "stages": stages,
        "warm_latency": latency,
        "first_inference_ms": first["total_ms"] if first else None,
        "failures": failures,
        "failure_counts": {key: len(value) for key, value in failures.items()},
    }, evaluations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline, baseline_evaluations = evaluate(args.baseline, args.data)
    report = {"baseline": baseline}
    if args.candidate:
        candidate, candidate_evaluations = evaluate(args.candidate, args.data)
        baseline_meta, candidate_meta = baseline["metadata"], candidate["metadata"]
        _validate_paired_identity(baseline_meta, candidate_meta)
        report["candidate"] = candidate
        report["paired_rerank"] = {
            metric: asdict(
                paired_bootstrap_comparison(
                    baseline_evaluations["rerank"],
                    candidate_evaluations["rerank"],
                    metric=metric,
                    sample_count=10000,
                    seed=20261021,
                )
            )
            for metric in ("nDCG@10", "Recall@20", "Recall@5", "MRR@10")
        }
    dump(args.output, report)
    for name in ("baseline", "candidate"):
        if name in report:
            print(
                name,
                json.dumps(
                    {stage: stats["metrics"] for stage, stats in report[name]["stages"].items()}
                ),
            )
    if "paired_rerank" in report:
        print("paired_rerank", json.dumps(report["paired_rerank"]))


if __name__ == "__main__":
    main()
