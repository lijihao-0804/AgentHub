"""Score the paired tokenizer-IDF and BGE-M3 learned-sparse retrieval runs.

This is a local public-benchmark mechanism comparison, not a platform Formal
Experiment and not a change to production retrieval or snapshot semantics.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from math import isclose, isfinite
from pathlib import Path
from random import Random
from statistics import fmean
from typing import Any

from benchmarks.public_rag.data import load_scifact, query_groups
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.scoring import (
    AggregateEvaluation,
    evaluate_questions,
    paired_bootstrap_comparison,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "data/external/rag-public-20261021/scifact/extracted/scifact"
ARMS = ("baseline", "learned")
STAGES = ("dense", "sparse", "fusion", "selection", "rerank")
STAGE_CAPS = {"dense": 30, "sparse": 30, "fusion": 20, "selection": 20, "rerank": 20}
EXPECTED_QUERY_COUNTS = {"train": 809, "test": 300}
BUDGETS = {
    "dense_top_k": 30,
    "sparse_top_k": 30,
    "candidate_top_k": 20,
    "rerank_top_k": 20,
    "final_top_k": 20,
    "rrf_k": 60,
}
EXPECTED_CONTROLS = {
    **BUDGETS,
    "dense_model": "BAAI/bge-m3",
    "reranker_model": "BAAI/bge-reranker-v2-m3",
    "embedding_batch_size": 8,
    "reranker_batch_size": 8,
    "chunk_size_chars": 1200,
    "chunk_overlap_chars": 200,
    "max_chunks_per_document": None,
    "dense_search_policy": "Both arms use the baseline dense ANN graph",
}
EXPECTED_PROFILES = {
    "baseline": {
        "encoder": "TOKENIZER_LOG_TF_IDF",
        "modifier": "idf",
        "collection": "rag_scifact_20261021",
    },
    "learned": {
        "encoder": "BGE_M3_LEARNED_V1",
        "modifier": "none",
        "collection": "rag_scifact_learned_20261022",
    },
}
SELECTION_KEYS = (
    "min_ndcg10_gain",
    "max_recall5_loss",
    "max_recall20_loss",
    "require_primary_ci_above_zero",
)
EXPECTED_SELECTION_RULE = {
    "min_ndcg10_gain": 0.005,
    "max_recall5_loss": 0.005,
    "max_recall20_loss": 0.005,
    "require_primary_ci_above_zero": True,
}
PRIMARY_METRIC = "nDCG@10"
SECONDARY_METRICS = ("Recall@5", "Recall@20", "MRR@10")
METRIC_PROJECTION_CHUNKS = 20
BOOTSTRAP_SAMPLES = 10_000
HIT_FIELDS = frozenset({"chunk_id", "document_id", "score"})
DENSE_SCORE_ABS_TOLERANCE = 1e-6
DENSE_SCORE_REL_TOLERANCE = 1e-9


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read trace: {path}") from exc
    rows = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"{path.name} line {line_number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {line_number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {line_number} must be a JSON object")
        rows.append(row)
    return rows


def _dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _finite(value: object, *, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source} must be numeric")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{source} must be finite")
    return number


def _nonnegative(value: object, *, source: str) -> float:
    number = _finite(value, source=source)
    if number < 0:
        raise ValueError(f"{source} must be non-negative")
    return number


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("schema_version") != 1:
        raise ValueError("protocol schema_version must be 1")
    seed = protocol.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("protocol seed must be an integer")
    if not isinstance(protocol.get("snapshot_id"), str) or not protocol["snapshot_id"]:
        raise ValueError("protocol snapshot_id must be a non-empty string")
    if protocol.get("arms") != EXPECTED_PROFILES:
        raise ValueError("protocol index profiles differ from the two registered mechanisms")
    for key, expected in BUDGETS.items():
        if protocol.get(key) != expected:
            raise ValueError(f"protocol {key} must be {expected}")
    if not isinstance(protocol.get("model_revision"), str) or not protocol["model_revision"]:
        raise ValueError("protocol model_revision must be a non-empty string")
    rule = protocol.get("selection_rule")
    if not isinstance(rule, Mapping):
        raise ValueError("protocol requires a selection_rule object")
    for key in SELECTION_KEYS[:-1]:
        _nonnegative(rule.get(key), source=f"protocol selection_rule.{key}")
    if not isinstance(rule.get("require_primary_ci_above_zero"), bool):
        raise ValueError("protocol selection_rule.require_primary_ci_above_zero must be boolean")
    for key, expected in EXPECTED_SELECTION_RULE.items():
        if rule.get(key) != expected:
            raise ValueError(f"protocol selection_rule.{key} differs from the predeclared rule")


def _validate_metadata(
    metadata: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
    phase: str,
    arm: str,
) -> None:
    split = "train" if phase == "dev" else "test"
    expected = {
        "schema_version": 4,
        "arm": arm,
        "phase": phase,
        "split": split,
        "queries": EXPECTED_QUERY_COUNTS[split],
        "seed": protocol["seed"],
        "snapshot_id": protocol["snapshot_id"],
        "common_controls": EXPECTED_CONTROLS,
        "index_profile": EXPECTED_PROFILES[arm],
        "paired_interleaved": True,
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise ValueError(f"{arm} metadata {key} differs from the registered run")


def _load_runs(
    input_dir: Path,
    *,
    phase: str,
    protocol: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    runs = {}
    for arm in ARMS:
        trace_path = input_dir / f"{phase}-{arm}.jsonl"
        metadata_path = input_dir / f"{phase}-{arm}.meta.json"
        metadata = _read_json(metadata_path)
        _validate_metadata(metadata, protocol=protocol, phase=phase, arm=arm)
        rows = _read_jsonl(trace_path)
        if len(rows) != metadata["queries"]:
            raise ValueError(
                f"{trace_path.name} has {len(rows)} rows; metadata declares {metadata['queries']}"
            )
        indexed: dict[str, dict[str, Any]] = {}
        for row in rows:
            query_id = row.get("query_id")
            if not isinstance(query_id, str) or not query_id:
                raise ValueError(f"{trace_path.name} has an invalid query_id")
            if query_id in indexed:
                raise ValueError(f"duplicate query ID in {trace_path.name}: {query_id}")
            indexed[query_id] = row
        runs[arm] = (metadata, indexed)
    return runs


def _validate_hits(
    ranking: object,
    *,
    source: str,
    mapping: dict[str, str],
) -> list[tuple[str, str, float]]:
    if not isinstance(ranking, list):
        raise ValueError(f"{source} ranking must be a list")
    hits = []
    for hit in ranking:
        if not isinstance(hit, Mapping) or set(hit) != HIT_FIELDS:
            raise ValueError(f"{source} has malformed ranking hits")
        chunk_id, document_id = hit.get("chunk_id"), hit.get("document_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise ValueError(f"{source} has an invalid chunk_id")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError(f"{source} has an invalid document_id")
        prior = mapping.setdefault(chunk_id, document_id)
        if prior != document_id:
            raise ValueError(f"chunk-to-document mapping differs across stages or arms: {chunk_id}")
        hits.append((chunk_id, document_id, _finite(hit.get("score"), source=f"{source} score")))
    chunk_ids = [chunk_id for chunk_id, _, _ in hits]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError(f"{source} contains duplicate chunk IDs")
    return hits


def _validate_rows(
    runs: Mapping[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
) -> None:
    query_sets = {frozenset(rows) for _, rows in runs.values()}
    if len(query_sets) != 1:
        raise ValueError("baseline and learned traces contain different query sets")
    query_ids = set(next(iter(query_sets)))
    if not query_ids:
        raise ValueError("paired traces are empty")
    chunk_documents: dict[str, str] = {}
    ordinals = None
    for query_id in sorted(query_ids):
        pair_orders = None
        query_ordinals = []
        for arm in ARMS:
            row = runs[arm][1][query_id]
            ordinal = row.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise ValueError(f"{arm} {query_id} has an invalid ordinal")
            first_inference = row.get("first_inference")
            if not isinstance(first_inference, bool) or first_inference != (ordinal == 0):
                raise ValueError(f"{arm} {query_id} warmup marker disagrees with ordinal")
            order = row.get("pair_order")
            if (
                not isinstance(order, list)
                or len(order) != 2
                or any(not isinstance(name, str) for name in order)
                or set(order) != set(ARMS)
            ):
                raise ValueError(f"{arm} {query_id} pair_order must permute the two arms")
            if pair_orders is None:
                pair_orders = order
            elif order != pair_orders:
                raise ValueError(f"pair_order differs between arms for {query_id}")
            query_ordinals.append(ordinal)
            _nonnegative(row.get("total_ms"), source=f"{arm} {query_id} total_ms")
            output_characters = row.get("output_characters")
            if (
                isinstance(output_characters, bool)
                or not isinstance(output_characters, int)
                or output_characters < 0
            ):
                raise ValueError(f"{arm} {query_id} has invalid output_characters")
            db_ms = row.get("db_ms")
            if not isinstance(db_ms, Mapping) or not set(db_ms).issubset(
                {"snapshot_scope", "chunk_load"}
            ):
                raise ValueError(f"{arm} {query_id} has invalid db_ms")
            for name, value in db_ms.items():
                _nonnegative(value, source=f"{arm} {query_id} db_ms.{name}")
            stages = row.get("stages")
            if not isinstance(stages, Mapping) or set(stages) != set(STAGES):
                raise ValueError(f"{arm} {query_id} must contain exactly the five stages")
            stage_chunks: dict[str, list[str]] = {}
            for stage in STAGES:
                payload = stages[stage]
                if not isinstance(payload, Mapping) or set(payload) != {"latency_ms", "ranking"}:
                    raise ValueError(f"{arm} {query_id} {stage} has malformed payload")
                _nonnegative(
                    payload.get("latency_ms"),
                    source=f"{arm} {query_id} {stage}.latency_ms",
                )
                source = f"{arm} {query_id} {stage}"
                hits = _validate_hits(
                    payload.get("ranking"),
                    source=source,
                    mapping=chunk_documents,
                )
                stage_chunks[stage] = [chunk_id for chunk_id, _, _ in hits]
                if len(hits) > STAGE_CAPS[stage]:
                    raise ValueError(
                        f"{source} contains {len(hits)} chunks; maximum is {STAGE_CAPS[stage]}"
                    )
            if not set(stage_chunks["selection"]).issubset(stage_chunks["fusion"]):
                raise ValueError(f"{arm} {query_id} selection is not a subset of fusion")
            if not set(stage_chunks["rerank"]).issubset(stage_chunks["selection"]):
                raise ValueError(f"{arm} {query_id} rerank is not a subset of selection")
            for field in ("sparse_encoding_ms", "sparse_nnz"):
                if field in row:
                    _nonnegative(row[field], source=f"{arm} {query_id} {field}")
                    if field == "sparse_nnz" and not isinstance(row[field], int):
                        raise ValueError(f"{arm} {query_id} sparse_nnz must be an integer")
        if query_ordinals[0] != query_ordinals[1]:
            raise ValueError(f"paired ordinal differs for {query_id}")
        if ordinals is None:
            ordinals = []
        ordinals.append(query_ordinals[0])
    if sorted(ordinals or []) != list(range(len(query_ids))):
        raise ValueError("ordinals must cover every query exactly once")


def _validate_dense_control(
    runs: Mapping[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    baseline_rows = runs["baseline"][1]
    learned_rows = runs["learned"][1]
    query_ids = sorted(baseline_rows)
    chunks_compared = 0
    for query_id in query_ids:
        baseline = baseline_rows[query_id]["stages"]["dense"]["ranking"]
        learned = learned_rows[query_id]["stages"]["dense"]["ranking"]
        if len(baseline) != len(learned):
            raise ValueError(
                f"dense control failed for {query_id}: ranking lengths differ "
                f"({len(baseline)} vs {len(learned)})"
            )
        for rank, (baseline_hit, learned_hit) in enumerate(
            zip(baseline, learned, strict=True),
            start=1,
        ):
            if baseline_hit["chunk_id"] != learned_hit["chunk_id"]:
                raise ValueError(
                    f"dense control failed for {query_id} at rank {rank}: "
                    f"{baseline_hit['chunk_id']} != {learned_hit['chunk_id']}"
                )
            if not isclose(
                float(baseline_hit["score"]),
                float(learned_hit["score"]),
                rel_tol=DENSE_SCORE_REL_TOLERANCE,
                abs_tol=DENSE_SCORE_ABS_TOLERANCE,
            ):
                raise ValueError(
                    f"dense control failed for {query_id} at rank {rank}: "
                    "scores differ beyond floating-point tolerance"
                )
        chunks_compared += len(baseline)
    return {
        "dense_control_matched": True,
        "queries_checked": len(query_ids),
        "chunk_rankings_compared": len(query_ids),
        "chunks_compared": chunks_compared,
        "score_abs_tolerance": DENSE_SCORE_ABS_TOLERANCE,
        "score_rel_tolerance": DENSE_SCORE_REL_TOLERANCE,
        "policy": EXPECTED_CONTROLS["dense_search_policy"],
        "interpretation": (
            "Dense candidate order is shared across arms, so differences from fusion "
            "onward are attributable to the sparse mechanism rather than a rebuilt ANN graph."
        ),
    }


def _validate_seeded_order(
    runs: Mapping[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
    qrels: Mapping[str, Mapping[str, int]],
    seed: int,
) -> None:
    expected_query_ids = sorted(qrels)
    Random(seed).shuffle(expected_query_ids)
    order_rng = Random(seed + 23)
    for ordinal, query_id in enumerate(expected_query_ids):
        expected_order = list(ARMS)
        order_rng.shuffle(expected_order)
        for arm in ARMS:
            row = runs[arm][1][query_id]
            if row["ordinal"] != ordinal:
                raise ValueError(f"{arm} ordinal differs from seeded query order for {query_id}")
            if row["pair_order"] != expected_order:
                raise ValueError(f"{arm} pair_order differs from seed+23 for {query_id}")


def _record_label_access(
    input_dir: Path,
    *,
    phase: str,
    split: str,
    protocol_path: Path,
    frozen_path: Path | None,
) -> Path:
    path = input_dir / "label-access.jsonl"
    record = {
        "event": (
            "LEARNED_DEV_SCORING_LABEL_ACCESS"
            if phase == "dev"
            else "LEARNED_EXPOSED_TEST_REGRESSION_SCORING_LABEL_ACCESS"
        ),
        "at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "split": split,
        "protocol": str(protocol_path),
        "formal_experiment": False,
        "blind": False,
    }
    if frozen_path is not None:
        record["frozen_selection"] = str(frozen_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _validate_frozen(path: Path, protocol: Mapping[str, Any]) -> dict[str, Any]:
    frozen = _read_json(path)
    if frozen.get("protocol") != protocol:
        raise ValueError("frozen selection protocol differs from the regression protocol")
    if frozen.get("selected_arm") not in ARMS:
        raise ValueError("frozen selection names an unknown arm")
    if not isinstance(frozen.get("reason"), str) or not frozen["reason"]:
        raise ValueError("frozen selection requires a reason")
    return frozen


def _score_arm(
    rows: Mapping[str, Mapping[str, Any]],
    qrels: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, Any], dict[str, AggregateEvaluation]]:
    stages: dict[str, Any] = {}
    evaluations: dict[str, AggregateEvaluation] = {}
    for stage in STAGES:
        raw_by_query = {query_id: rows[query_id]["stages"][stage]["ranking"] for query_id in qrels}
        rankings = {
            query_id: tuple(
                hit["document_id"] for hit in raw_by_query[query_id][:METRIC_PROJECTION_CHUNKS]
            )
            for query_id in qrels
        }
        evaluation = evaluate_questions(qrels, rankings)
        evaluations[stage] = evaluation

        per_query: dict[str, Any] = {}
        candidate_coverages = []
        pooled_found = 0
        pooled_gold = 0
        for query_id in qrels:
            gold = {document_id for document_id, grade in qrels[query_id].items() if grade > 0}
            raw = raw_by_query[query_id]
            raw_chunks = [hit["chunk_id"] for hit in raw]
            raw_documents = {hit["document_id"] for hit in raw}
            found = raw_documents.intersection(gold)
            coverage = len(found) / len(gold) if gold else None
            if coverage is not None:
                candidate_coverages.append(coverage)
                pooled_found += len(found)
                pooled_gold += len(gold)
            question = evaluation.questions[query_id]
            item: dict[str, Any] = {
                "evaluated": question.evaluated,
                "metrics": dict(question.metrics),
                "metric_projection_gold_coverage": question.gold_coverage,
                "candidate_pool": {
                    "raw_chunks": len(raw),
                    "unique_chunks": len(set(raw_chunks)),
                    "unique_documents": len(raw_documents),
                    "duplicate_chunks": len(raw_chunks) - len(set(raw_chunks)),
                    "gold_found": len(found),
                    "gold_total": len(gold),
                    "gold_coverage": coverage,
                },
            }
            if stage == "rerank":
                item["ranked_chunk_ids"] = raw_chunks[:METRIC_PROJECTION_CHUNKS]
                item["ranked_documents"] = list(question.ranking)
                item["gold_documents"] = list(question.gold_documents)
                item["missing_documents"] = list(question.missing_documents)
            per_query[query_id] = item

        stages[stage] = {
            "metrics": dict(evaluation.metrics),
            "evaluated_count": evaluation.evaluated_count,
            "skipped_unjudged_count": evaluation.skipped_unjudged_count,
            "skipped_no_positive_count": evaluation.skipped_no_positive_count,
            "coverage": {
                "metric_projection_raw_chunks": METRIC_PROJECTION_CHUNKS,
                "metric_projection_macro_gold_coverage": (
                    fmean(
                        question.gold_coverage
                        for question in evaluation.questions.values()
                        if question.evaluated
                    )
                    if evaluation.evaluated_count
                    else None
                ),
                "candidate_pool": {
                    "raw_chunks": sum(
                        item["candidate_pool"]["raw_chunks"] for item in per_query.values()
                    ),
                    "unique_documents": sum(
                        item["candidate_pool"]["unique_documents"] for item in per_query.values()
                    ),
                    "duplicate_chunks": sum(
                        item["candidate_pool"]["duplicate_chunks"] for item in per_query.values()
                    ),
                    "macro_gold_coverage": (
                        fmean(candidate_coverages) if candidate_coverages else None
                    ),
                    "pooled_gold_coverage": (pooled_found / pooled_gold if pooled_gold else None),
                },
            },
        }
    return {
        "stages": stages,
        "per_query": per_query,
        "ranking_projection": (
            "Each stage's first 20 raw chunks are scored; scoring.evaluate_questions "
            "deduplicates documents before applying Recall/MRR/nDCG cutoffs. Candidate-pool "
            "coverage reports the full actual stage ranking and is not a cutoff Recall metric."
        ),
    }, evaluations


def _timing_value(row: Mapping[str, Any], name: str) -> float:
    if name == "total":
        return float(row["total_ms"])
    return float(row["stages"][name]["latency_ms"])


def _timing_summary(values: Mapping[str, float]) -> dict[str, Any]:
    ordered = list(values.values())
    return {
        "n": len(ordered),
        "mean_ms": fmean(ordered) if ordered else None,
        "p50_ms": _quantile(ordered, 0.5),
        "p95_ms": _quantile(ordered, 0.95),
    }


def _paired_timing(
    baseline: Mapping[str, float],
    learned: Mapping[str, float],
    groups: Mapping[str, str],
    *,
    seed: int,
) -> dict[str, Any]:
    if not baseline:
        return {
            "n": 0,
            "baseline": _timing_summary({}),
            "learned": _timing_summary({}),
            "paired": None,
        }
    difference = grouped_difference(
        dict(baseline),
        dict(learned),
        dict(groups),
        samples=BOOTSTRAP_SAMPLES,
        seed=seed,
    )
    return {
        "n": len(baseline),
        "baseline": _timing_summary(baseline),
        "learned": _timing_summary(learned),
        "paired": {
            **difference,
            "sign": "learned minus baseline; negative is faster",
        },
    }


def _latency_report(
    runs: Mapping[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
    qrels: Mapping[str, Mapping[str, int]],
    seed: int,
) -> dict[str, Any]:
    baseline_rows, learned_rows = runs["baseline"][1], runs["learned"][1]
    warm = [query_id for query_id in qrels if baseline_rows[query_id]["ordinal"] > 0]
    excluded = sorted(query_id for query_id in qrels if baseline_rows[query_id]["ordinal"] == 0)
    groups = query_groups(dict(qrels))
    metrics: dict[str, Any] = {}
    strata = {
        "baseline_first": [
            query_id for query_id in warm if baseline_rows[query_id]["pair_order"][0] == "baseline"
        ],
        "baseline_after": [
            query_id for query_id in warm if baseline_rows[query_id]["pair_order"][0] == "learned"
        ],
    }
    for name in ("total", *STAGES):
        baseline = {query_id: _timing_value(baseline_rows[query_id], name) for query_id in warm}
        learned = {query_id: _timing_value(learned_rows[query_id], name) for query_id in warm}
        metrics[name] = {
            **_paired_timing(baseline, learned, groups, seed=seed),
            "pair_order_strata": {
                label: _paired_timing(
                    {query_id: baseline[query_id] for query_id in query_ids},
                    {query_id: learned[query_id] for query_id in query_ids},
                    groups,
                    seed=seed,
                )
                for label, query_ids in strata.items()
            },
        }
    return {
        "warm_query_count": len(warm),
        "excluded_ordinal_zero_queries": excluded,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "confidence": 0.95,
        "metrics": metrics,
        "optional_sparse_encoding_ms": _optional_sparse_encoding_ms(
            baseline_rows, learned_rows, warm
        ),
        "limitations": (
            "One randomized paired session on one machine; intervals describe query/source "
            "mix, not hardware or between-session variation."
        ),
    }


def _optional_sparse_encoding_ms(
    baseline_rows: Mapping[str, Mapping[str, Any]],
    learned_rows: Mapping[str, Mapping[str, Any]],
    warm: Sequence[str],
) -> dict[str, Any]:
    baseline = {
        query_id: float(baseline_rows[query_id]["sparse_encoding_ms"])
        for query_id in warm
        if "sparse_encoding_ms" in baseline_rows[query_id]
    }
    learned = {
        query_id: float(learned_rows[query_id]["sparse_encoding_ms"])
        for query_id in warm
        if "sparse_encoding_ms" in learned_rows[query_id]
    }
    complete = len(baseline) == len(warm) and len(learned) == len(warm)
    return {
        "per_query_complete": complete,
        "baseline_recorded_queries": len(baseline),
        "learned_recorded_queries": len(learned),
        "warm_query_count": len(warm),
        "summary": (
            {
                "baseline": _timing_summary(baseline),
                "learned": _timing_summary(learned),
            }
            if complete
            else None
        ),
    }


def _work_report(
    rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rerank_inputs = [len(row["stages"]["selection"]["ranking"]) for row in rows.values()]
    final_chunks = [len(row["stages"]["rerank"]["ranking"]) for row in rows.values()]
    output_characters = [row["output_characters"] for row in rows.values()]
    sparse_nnz = [row["sparse_nnz"] for row in rows.values() if "sparse_nnz" in row]
    return {
        "actual_rerank_input_chunks": {
            "total": sum(rerank_inputs),
            "mean": fmean(rerank_inputs),
            "p50": _quantile(rerank_inputs, 0.5),
            "p95": _quantile(rerank_inputs, 0.95),
            "max": max(rerank_inputs),
        },
        "final_raw_chunks": {
            "total": sum(final_chunks),
            "mean": fmean(final_chunks),
            "p50": _quantile(final_chunks, 0.5),
            "p95": _quantile(final_chunks, 0.95),
            "max": max(final_chunks),
        },
        "retrieved_output_characters": {
            "total": sum(output_characters),
            "mean": fmean(output_characters),
            "p50": _quantile(output_characters, 0.5),
            "p95": _quantile(output_characters, 0.95),
        },
        "sparse_nnz_per_query": {
            "recorded_queries": len(sparse_nnz),
            "total_queries": len(rows),
            "mean": fmean(sparse_nnz) if sparse_nnz else None,
            "p50": _quantile(sparse_nnz, 0.5),
            "p95": _quantile(sparse_nnz, 0.95),
            "max": max(sparse_nnz) if sparse_nnz else None,
        },
        "estimated_tokens": None,
        "estimated_dollars": None,
        "note": (
            "Only observed retrieval work is reported; token and dollar costs are not inferred."
        ),
    }


def _prepared_index_nnz(
    input_dir: Path,
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    path = input_dir / "prepared-index.json"
    if not path.is_file():
        return {"available": False, "path": str(path)}
    manifest = _read_json(path)
    if manifest.get("protocol") != protocol:
        raise ValueError("prepared-index.json protocol differs")
    if manifest.get("snapshot_id") != protocol["snapshot_id"]:
        raise ValueError("prepared-index.json snapshot differs")
    if manifest.get("index_profile") != EXPECTED_PROFILES["learned"]:
        raise ValueError("prepared-index.json does not contain the learned profile")
    return {
        "available": True,
        "path": str(path),
        "chunks": manifest.get("chunks"),
        "mean_sparse_nnz": _nonnegative(
            manifest.get("mean_sparse_nnz"), source="prepared mean_sparse_nnz"
        ),
        "max_sparse_nnz": _nonnegative(
            manifest.get("max_sparse_nnz"), source="prepared max_sparse_nnz"
        ),
        "scope": "whole prepared corpus, not per-query",
    }


def _comparisons(
    evaluations: Mapping[str, Mapping[str, AggregateEvaluation]],
    seed: int,
) -> dict[str, Any]:
    baseline = evaluations["baseline"]["rerank"]
    learned = evaluations["learned"]["rerank"]

    def compare(metric: str) -> dict[str, Any]:
        payload = asdict(
            paired_bootstrap_comparison(
                baseline,
                learned,
                metric=metric,
                sample_count=BOOTSTRAP_SAMPLES,
                confidence=0.95,
                seed=seed,
            )
        )
        payload["interval"] = "paired percentile bootstrap over gold-overlap query clusters"
        return payload

    return {
        "primary": {
            "metric": PRIMARY_METRIC,
            "single_comparison": True,
            **compare(PRIMARY_METRIC),
        },
        "secondary_exploratory": {metric: compare(metric) for metric in SECONDARY_METRICS},
    }


def _select_arm(protocol: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    rule = protocol["selection_rule"]
    baseline = report["arms"]["baseline"]["stages"]["rerank"]["metrics"]
    learned = report["arms"]["learned"]["stages"]["rerank"]["metrics"]
    primary = report["comparisons"]["primary"]
    point_gain = learned[PRIMARY_METRIC] - baseline[PRIMARY_METRIC]
    recall5_loss = max(0.0, baseline["Recall@5"] - learned["Recall@5"])
    recall20_loss = max(0.0, baseline["Recall@20"] - learned["Recall@20"])
    ci_required = rule["require_primary_ci_above_zero"]
    checks = {
        "min_ndcg10_gain": {
            "observed": point_gain,
            "threshold": float(rule["min_ndcg10_gain"]),
            "passed": point_gain >= float(rule["min_ndcg10_gain"]),
        },
        "primary_ci_above_zero": {
            "observed_ci95_low": primary["ci_low"],
            "required": ci_required,
            "passed": primary["ci_low"] > 0 if ci_required else True,
        },
        "max_recall5_loss": {
            "observed_loss": recall5_loss,
            "threshold": float(rule["max_recall5_loss"]),
            "passed": recall5_loss <= float(rule["max_recall5_loss"]),
        },
        "max_recall20_loss": {
            "observed_loss": recall20_loss,
            "threshold": float(rule["max_recall20_loss"]),
            "passed": recall20_loss <= float(rule["max_recall20_loss"]),
        },
    }
    failed = [name for name, result in checks.items() if not result["passed"]]
    selected_arm = "learned" if not failed else "baseline"
    if selected_arm == "learned":
        reason = (
            "learned cleared the predeclared nDCG@10 gain, primary 95% CI, "
            "Recall@5 loss, and Recall@20 loss rules."
        )
    else:
        reason = "baseline retained because these frozen DEV rules failed: " + ", ".join(failed)
    return {
        "rule": dict(rule),
        "checks": checks,
        "selected_arm": selected_arm,
        "reason": reason,
    }


def _freeze_selection(
    input_dir: Path,
    protocol: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> Path:
    path = input_dir / "frozen-selection.json"
    payload = {
        "schema_version": 1,
        "status": "FROZEN_DEV_SELECTION",
        "phase": "dev",
        "split": "train",
        "protocol": dict(protocol),
        "selected_arm": selection["selected_arm"],
        "reason": selection["reason"],
        "selection": dict(selection),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    if path.exists():
        existing = _read_json(path)
        for key in ("status", "phase", "split", "protocol", "selected_arm", "reason", "selection"):
            if existing.get(key) != payload[key]:
                raise ValueError("existing frozen-selection.json differs from this DEV selection")
        return path
    _dump(path, payload)
    return path


def build_report(
    input_dir: Path,
    *,
    phase: str,
    protocol_path: Path,
    data_path: Path | None = None,
    frozen_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Load, audit, validate, score, and optionally freeze one two-arm phase."""

    if phase not in {"dev", "regression"}:
        raise ValueError("phase must be dev or regression")
    if phase == "dev" and frozen_path is not None:
        raise ValueError("DEV creates its own frozen selection; do not pass --frozen")
    if phase == "regression" and frozen_path is None:
        raise ValueError("regression requires --frozen")
    protocol = _read_json(protocol_path)
    _validate_protocol(protocol)
    runs = _load_runs(input_dir, phase=phase, protocol=protocol)
    _validate_rows(runs)
    dense_control = _validate_dense_control(runs)
    frozen = (
        _validate_frozen(frozen_path, protocol)
        if phase == "regression" and frozen_path is not None
        else None
    )

    split = "train" if phase == "dev" else "test"
    label_path = _record_label_access(
        input_dir,
        phase=phase,
        split=split,
        protocol_path=protocol_path,
        frozen_path=frozen_path,
    )
    source = load_scifact(data_path or DEFAULT_DATA, split)
    if len(source.qrels) != EXPECTED_QUERY_COUNTS[split]:
        raise ValueError(
            f"official {split} split has {len(source.qrels)} queries; "
            f"expected {EXPECTED_QUERY_COUNTS[split]}"
        )
    if set(source.queries) != set(source.qrels):
        raise ValueError("official split queries and qrels identify different query sets")
    trace_queries = set(runs["baseline"][1])
    if trace_queries != set(source.qrels):
        missing = sorted(set(source.qrels) - trace_queries)[:5]
        extra = sorted(trace_queries - set(source.qrels))[:5]
        raise ValueError(f"complete official split required; missing={missing}, extra={extra}")
    _validate_seeded_order(runs, source.qrels, protocol["seed"])
    arms: dict[str, Any] = {}
    evaluations: dict[str, dict[str, AggregateEvaluation]] = {}
    for arm in ARMS:
        metadata, rows = runs[arm]
        arm_report, arm_evaluations = _score_arm(rows, source.qrels)
        arms[arm] = {
            "metadata": metadata,
            "input": f"{phase}-{arm}.jsonl",
            **arm_report,
            "observed_work": _work_report(rows),
        }
        evaluations[arm] = arm_evaluations

    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "split": split,
        "query_count": len(source.qrels),
        "arms": arms,
        "dense_control": dense_control,
        "comparisons": _comparisons(evaluations, protocol["seed"]),
        "latency": _latency_report(runs, source.qrels, protocol["seed"]),
        "prepared_index_sparse_nnz": _prepared_index_nnz(input_dir, protocol),
        "scope": {
            "benchmark": "SciFact retrieval-only learned-sparse mechanism comparison",
            "blind": False,
            "exposed_test": split == "test",
            "test_status": (
                "train labels used for DEV"
                if split == "train"
                else "previously exposed test labels; regression only, not blind"
            ),
            "formal_experiment": False,
            "production_default_changed": False,
            "index_profile_note": (
                "index_profile is benchmark run metadata, not KnowledgeSnapshot schema v2"
            ),
            "label_access_audit": str(label_path),
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if phase == "dev":
        selection = _select_arm(protocol, report)
        frozen_path = _freeze_selection(input_dir, protocol, selection)
        selection["frozen_path"] = str(frozen_path)
        report["selection"] = selection
    else:
        assert frozen is not None
        report["selection"] = {
            "selected_arm": frozen["selected_arm"],
            "reason": frozen["reason"],
            "frozen_path": str(frozen_path),
            "reselected": False,
        }
    return report, label_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--phase", choices=("dev", "regression"), required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    if args.phase == "regression" and args.frozen is None:
        parser.error("regression requires --frozen")
    if args.phase == "dev" and args.frozen is not None:
        parser.error("dev creates its own frozen selection; do not pass --frozen")
    report, _ = build_report(
        args.input,
        phase=args.phase,
        protocol_path=args.protocol,
        data_path=args.data,
        frozen_path=args.frozen,
    )
    _dump(args.output, report)
    print(
        json.dumps(
            {
                "phase": report["phase"],
                "queries": report["query_count"],
                "selected_arm": report["selection"]["selected_arm"],
                "primary": report["comparisons"]["primary"],
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
