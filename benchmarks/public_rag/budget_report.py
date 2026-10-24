"""Score budget and diversity ablation traces without running retrieval.

The DEV screen replays each arm from a fully scored superset. The regression
phase reports the previously exposed test set from a separately executed,
randomly interleaved five-arm run using the frozen DEV selection.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from random import Random
from statistics import fmean
from typing import Any

from benchmarks.public_rag.budget_ablation import ARMS, replay_arm
from benchmarks.public_rag.data import load_scifact, query_groups
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.scifact_report import quantile
from benchmarks.public_rag.scifact_runner import DATA, dump
from benchmarks.public_rag.scoring import (
    AggregateEvaluation,
    evaluate_questions,
    paired_bootstrap_comparison,
)

ARM_NAMES = tuple(ARMS)
EXPECTED_QUERY_COUNTS = {"train": 809, "test": 300}
FINAL_TOP_K = 20
SUPERSET_TOP_K = 60
PRIMARY_METRIC = "nDCG@10"
EXPLORATORY_METRICS = ("Recall@5", "Recall@20", "MRR@10")
BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20261022
BONFERRONI_CONFIDENCE = 0.9875
SELECTION_RULE_KEYS = (
    "ndcg10_noninferiority_margin",
    "recall5_noninferiority_margin",
    "minimum_ndcg10_or_recall20_gain",
    "near_best_ndcg10_margin",
)

METRIC_DEFINITIONS = {
    "document_metrics": (
        "Use the first 20 final raw chunks, preserve first occurrence while deduplicating "
        "documents, then apply cutoffs through scoring.evaluate_questions."
    ),
    "selection": (
        "Diagnostics over the reranker input actually selected by each arm; "
        "candidate_oracle_coverage is the macro mean fraction of positive gold documents "
        "present anywhere in that selected pool, as if its document order were perfect. "
        "Pooled candidate coverage weights gold documents instead of queries."
    ),
    "final_raw_chunks@5_gold_coverage": (
        "Mean fraction of positive gold documents represented by the first five raw "
        "rerank chunks. This diagnostic is not document Recall@5."
    ),
    "cost": (
        "Observed reranker input chunks and, for actual regression runs, retrieved output "
        "characters. Token and dollar estimates are not inferred."
    ),
}


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


def _rows_by_query(rows: Sequence[dict[str, Any]], path: Path) -> dict[str, dict[str, Any]]:
    indexed = {}
    for line_number, row in enumerate(rows, start=1):
        query_id = row.get("query_id")
        if not isinstance(query_id, str) or not query_id:
            raise ValueError(f"{path.name} line {line_number} has invalid query_id")
        if query_id in indexed:
            raise ValueError(f"duplicate query ID in {path.name}: {query_id}")
        indexed[query_id] = row
    return indexed


def _validate_complete_split(
    rows: Mapping[str, Mapping[str, Any]],
    qrels: Mapping[str, Mapping[str, int]],
    *,
    declared_count: int,
    source: str,
    expected_count: int,
) -> None:
    if declared_count != expected_count:
        raise ValueError(
            f"{source} metadata declares {declared_count} queries; expected {expected_count}"
        )
    if len(qrels) != expected_count:
        raise ValueError(f"official split has {len(qrels)} queries; expected {expected_count}")
    expected = set(qrels)
    actual = set(rows)
    if len(actual) != expected_count:
        raise ValueError(
            f"{source} does not cover the complete official split: "
            f"unique_queries={len(actual)}, expected={expected_count}"
        )
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={missing[:5]}")
        if extra:
            details.append(f"extra={extra[:5]}")
        raise ValueError(
            f"{source} does not cover the complete official split: {', '.join(details)}"
        )


def _rule_number(protocol: Mapping[str, Any], key: str) -> float:
    rule = protocol.get("selection_rule")
    if not isinstance(rule, Mapping):
        raise ValueError("protocol requires a selection_rule object")
    value = rule.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"protocol selection_rule.{key} must be numeric")
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"protocol selection_rule.{key} must be finite and non-negative")
    return number


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("arms") != ARMS:
        raise ValueError("protocol arms differ from budget_ablation.ARMS")
    if protocol.get("final_top_k") != FINAL_TOP_K:
        raise ValueError("protocol final_top_k must be 20")
    seed = protocol.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("protocol seed must be an integer")
    for key in SELECTION_RULE_KEYS:
        _rule_number(protocol, key)


def _record_label_access(
    input_dir: Path,
    *,
    phase: str,
    protocol_path: Path,
    frozen_path: Path | None = None,
) -> None:
    event = (
        "DEV_SCORING_LABEL_ACCESS"
        if phase == "screen"
        else "EXPOSED_TEST_REGRESSION_SCORING_LABEL_ACCESS"
    )
    input_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event": event,
        "at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "protocol": str(protocol_path),
    }
    if frozen_path is not None:
        record["frozen_selection"] = str(frozen_path)
    with (input_dir / "label-access.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_official_split(
    split: str,
    data_path: Path | None,
    *,
    input_dir: Path,
    protocol_path: Path,
    frozen_path: Path | None,
) -> Any:
    _record_label_access(
        input_dir,
        phase="regression" if split == "test" else "screen",
        protocol_path=protocol_path,
        frozen_path=frozen_path,
    )
    source = load_scifact(data_path or DATA, split)
    expected = EXPECTED_QUERY_COUNTS[split]
    if len(source.qrels) != expected:
        raise ValueError(
            f"official {split} split has {len(source.qrels)} queries; expected {expected}"
        )
    if set(source.queries) != set(source.qrels):
        raise ValueError(f"official {split} queries and qrels do not identify the same query set")
    return source


def _expected_query_and_arm_order(
    protocol: Mapping[str, Any],
    qrels: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, int], dict[str, tuple[str, ...]]]:
    """Reproduce budget_ablation's deterministic query and within-query arm order."""

    query_ids = sorted(qrels)
    Random(protocol["seed"]).shuffle(query_ids)
    rng = Random(protocol["seed"] + 23)
    ordinals = {}
    orders = {}
    for ordinal, query_id in enumerate(query_ids):
        order = list(ARM_NAMES)
        rng.shuffle(order)
        ordinals[query_id] = ordinal
        orders[query_id] = tuple(order)
    return ordinals, orders


def _validate_screen_metadata(
    metadata: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> None:
    expected_count = EXPECTED_QUERY_COUNTS["train"]
    if metadata.get("phase") != "screen" or metadata.get("split") != "train":
        raise ValueError("screen input must declare phase=screen and split=train")
    if metadata.get("arm") != "superset":
        raise ValueError("screen input must be the superset arm")
    if metadata.get("controls") != ARMS["budget60"]:
        raise ValueError("screen input controls differ from budget60")
    if metadata.get("final_top_k") != SUPERSET_TOP_K:
        raise ValueError("screen input must expose all 60 final chunks")
    if metadata.get("queries") != expected_count:
        raise ValueError(f"screen metadata must declare {expected_count} train queries")
    if metadata.get("protocol") != protocol:
        raise ValueError("screen metadata protocol differs")
    if metadata.get("snapshot_id") != protocol.get("snapshot_id"):
        raise ValueError("screen metadata snapshot differs from protocol")
    if not isinstance(metadata.get("retrieval_identity"), Mapping):
        raise ValueError("screen metadata requires retrieval_identity")


def _validate_common_metadata(
    metadata_by_arm: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    expected_count = EXPECTED_QUERY_COUNTS["test"]
    for arm, metadata in metadata_by_arm.items():
        if metadata.get("phase") != "regression" or metadata.get("split") != "test":
            raise ValueError(f"{arm} must declare phase=regression and split=test")
        if metadata.get("arm") != arm:
            raise ValueError(f"{arm} metadata names a different arm")
        if metadata.get("controls") != ARMS[arm]:
            raise ValueError(f"{arm} metadata controls differ")
        if metadata.get("final_top_k") != FINAL_TOP_K:
            raise ValueError(f"{arm} metadata must use final_top_k=20")
        if metadata.get("queries") != expected_count:
            raise ValueError(f"{arm} metadata must declare {expected_count} test queries")
        if metadata.get("protocol") != protocol:
            raise ValueError(f"{arm} metadata protocol differs")
        if metadata.get("paired_interleaved") is not True:
            raise ValueError(f"{arm} metadata must declare paired_interleaved=True")
        if metadata.get("snapshot_id") != protocol.get("snapshot_id"):
            raise ValueError(f"{arm} metadata snapshot differs from protocol")
        if not isinstance(metadata.get("retrieval_identity"), Mapping):
            raise ValueError(f"{arm} metadata requires retrieval_identity")

    identity: dict[str, Any] = {}
    for key in ("snapshot_id", "retrieval_identity", "git_commit", "git_dirty", "device", "torch"):
        available = [key in metadata for metadata in metadata_by_arm.values()]
        if not any(available):
            continue
        if not all(available):
            raise ValueError(f"regression metadata is missing {key} for some arms")
        values = [metadata[key] for metadata in metadata_by_arm.values()]
        if any(value != values[0] for value in values[1:]):
            raise ValueError(f"regression metadata differs across arms: {key}")
        identity[key] = values[0]
    return identity


def _ranking(payload: Mapping[str, Any], stage: str, *, source: str) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, Mapping):
        raise ValueError(f"{source} has invalid stages")
    value = stages.get(stage)
    if not isinstance(value, Mapping) or not isinstance(value.get("ranking"), list):
        raise ValueError(f"{source} requires stages.{stage}.ranking")
    ranking = value["ranking"]
    if not all(isinstance(hit, Mapping) for hit in ranking):
        raise ValueError(f"{source} has malformed {stage} hits")
    return list(ranking)


def _validate_hit(
    hit: Mapping[str, Any],
    *,
    source: str,
    require_score: bool,
) -> tuple[str, str, float | None]:
    chunk_id = hit.get("chunk_id")
    document_id = hit.get("document_id")
    if not isinstance(chunk_id, str) or not chunk_id:
        raise ValueError(f"{source} has invalid chunk_id")
    if not isinstance(document_id, str) or not document_id:
        raise ValueError(f"{source} has invalid document_id")
    score = hit.get("score")
    if require_score:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"{source} has non-numeric score")
        score = float(score)
        if not isfinite(score):
            raise ValueError(f"{source} has non-finite score")
    else:
        score = None
    return chunk_id, document_id, score


def _validate_superset_row(row: Mapping[str, Any], query_id: str) -> None:
    fusion = _ranking(row, "fusion", source=f"{query_id} screen fusion")
    rerank = _ranking(row, "rerank", source=f"{query_id} screen rerank")
    if len(fusion) > SUPERSET_TOP_K:
        raise ValueError(f"{query_id} fusion has more than 60 candidates")
    if len(rerank) > SUPERSET_TOP_K:
        raise ValueError(f"{query_id} rerank has more than 60 scored candidates")
    fusion_hits = [
        _validate_hit(hit, source=f"{query_id} screen fusion", require_score=False)
        for hit in fusion
    ]
    rerank_hits = [
        _validate_hit(hit, source=f"{query_id} screen rerank", require_score=True) for hit in rerank
    ]
    fusion_chunks = [chunk_id for chunk_id, _, _ in fusion_hits]
    rerank_chunks = [chunk_id for chunk_id, _, _ in rerank_hits]
    if len(set(fusion_chunks)) != len(fusion_chunks):
        raise ValueError(f"{query_id} fusion contains duplicate chunk IDs")
    if len(set(rerank_chunks)) != len(rerank_chunks):
        raise ValueError(f"{query_id} rerank contains duplicate chunk IDs")
    if set(fusion_chunks) != set(rerank_chunks):
        raise ValueError(f"{query_id} screen requires one rerank score for every fused candidate")
    fusion_documents = {chunk_id: document_id for chunk_id, document_id, _ in fusion_hits}
    for chunk_id, document_id, _ in rerank_hits:
        if fusion_documents[chunk_id] != document_id:
            raise ValueError(f"{query_id} chunk-to-document mapping differs between stages")


def _validate_screen_order(
    rows: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
    qrels: Mapping[str, Mapping[str, int]],
) -> None:
    expected_ordinals, _ = _expected_query_and_arm_order(protocol, qrels)
    for query_id, expected in expected_ordinals.items():
        row = rows[query_id]
        if row.get("ordinal") != expected:
            raise ValueError(f"superset ordinal differs from seeded query order for {query_id}")
        if row.get("first_inference") is not (expected == 0):
            raise ValueError(f"superset warmup marker differs for {query_id}")
        if row.get("pair_order") != ["superset"]:
            raise ValueError(f"superset pair_order is invalid for {query_id}")


def _validate_regression_row(
    row: Mapping[str, Any],
    *,
    arm: str,
    query_id: str,
) -> None:
    ordinal = row.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError(f"{arm} has invalid ordinal for {query_id}")
    first_inference = row.get("first_inference")
    if not isinstance(first_inference, bool):
        raise ValueError(f"{arm} has invalid first_inference for {query_id}")
    if first_inference != (ordinal == 0):
        raise ValueError(f"{arm} first_inference disagrees with ordinal for {query_id}")
    order = row.get("pair_order")
    if not isinstance(order, list) or len(order) != len(ARM_NAMES) or set(order) != set(ARM_NAMES):
        raise ValueError(f"{arm} pair_order is not a five-arm permutation for {query_id}")
    _finite_number(row.get("total_ms"), source=f"{arm} total_ms for {query_id}")
    output_characters = row.get("output_characters")
    if (
        isinstance(output_characters, bool)
        or not isinstance(output_characters, int)
        or output_characters < 0
    ):
        raise ValueError(f"{arm} has invalid output_characters for {query_id}")
    stages = row.get("stages")
    if not isinstance(stages, Mapping):
        raise ValueError(f"{arm} has invalid stages for {query_id}")
    rerank_stage = stages.get("rerank")
    if not isinstance(rerank_stage, Mapping):
        raise ValueError(f"{arm} requires rerank stage for {query_id}")
    _finite_number(rerank_stage.get("latency_ms"), source=f"{arm} rerank latency for {query_id}")
    fusion = _ranking(row, "fusion", source=f"{arm} fusion for {query_id}")
    selection = _ranking(row, "selection", source=f"{arm} selection for {query_id}")
    rerank = _ranking(row, "rerank", source=f"{arm} rerank for {query_id}")
    if len(selection) > ARMS[arm]["rerank_top_k"]:
        raise ValueError(f"{arm} reranker input exceeds its rerank budget for {query_id}")
    if len(rerank) != FINAL_TOP_K:
        raise ValueError(
            f"{arm} regression requires exactly 20 final chunks for {query_id}; found {len(rerank)}"
        )

    chunk_documents = {}
    for stage, hits in (("fusion", fusion), ("selection", selection), ("rerank", rerank)):
        for hit in hits:
            chunk_id, document_id, _ = _validate_hit(
                hit,
                source=f"{arm} {stage} for {query_id}",
                require_score=stage != "fusion",
            )
            existing_document = chunk_documents.setdefault(chunk_id, document_id)
            if existing_document != document_id:
                raise ValueError(
                    f"{arm} chunk-to-document mapping differs across stages for {query_id}: "
                    f"{chunk_id}"
                )

    fusion_chunks = []
    selection_chunks = []
    rerank_chunks = []
    for stage, hits, destination in (
        ("fusion", fusion, fusion_chunks),
        ("selection", selection, selection_chunks),
        ("rerank", rerank, rerank_chunks),
    ):
        destination.extend(hit["chunk_id"] for hit in hits)
        if len(set(destination)) != len(destination):
            raise ValueError(f"{arm} {stage} contains duplicate chunk IDs for {query_id}")

    fusion_chunk_set = set(fusion_chunks)
    selection_chunk_set = set(selection_chunks)
    if not selection_chunk_set.issubset(fusion_chunk_set):
        raise ValueError(f"{arm} selection is not a subset of fusion for {query_id}")
    if not set(rerank_chunks).issubset(selection_chunk_set):
        raise ValueError(f"{arm} final chunks are not a subset of reranker input for {query_id}")


def _validate_regression_order(
    rows: Mapping[str, Mapping[str, dict[str, Any]]],
    protocol: Mapping[str, Any],
    qrels: Mapping[str, Mapping[str, int]],
) -> tuple[set[str], dict[str, int], dict[str, int]]:
    expected_ordinals, expected_orders = _expected_query_and_arm_order(protocol, qrels)
    first_query_ids = set()
    first_position_counts = {arm: 0 for arm in ARM_NAMES}
    pair_order_counts: Counter[tuple[str, ...]] = Counter()
    ordinals = []
    for query_id in qrels:
        reference = rows["baseline"][query_id]
        reference_order = tuple(reference["pair_order"])
        reference_ordinal = reference["ordinal"]
        reference_first = reference["first_inference"]
        if reference_ordinal != expected_ordinals[query_id]:
            raise ValueError(f"ordinal differs from seeded query order for {query_id}")
        if reference_order != expected_orders[query_id]:
            raise ValueError(f"pair_order differs from seeded interleaving for {query_id}")
        for arm in ARM_NAMES:
            row = rows[arm][query_id]
            if row["ordinal"] != reference_ordinal:
                raise ValueError(f"paired ordinal differs for {query_id}")
            if row["first_inference"] != reference_first:
                raise ValueError(f"first_inference differs for {query_id}")
            if tuple(row["pair_order"]) != reference_order:
                raise ValueError(f"pair_order differs between arms for {query_id}")
        ordinals.append(reference_ordinal)
        pair_order_counts[reference_order] += 1
        if reference_first:
            first_query_ids.add(query_id)
        else:
            first_position_counts[reference_order[0]] += 1
    expected_ordinals = list(range(len(qrels)))
    if sorted(ordinals) != expected_ordinals:
        raise ValueError("regression ordinals must cover 0 through 299 exactly once")
    if len(first_query_ids) != 1:
        raise ValueError("regression must identify exactly one ordinal-0 query")
    order_distribution = {
        " > ".join(order): count for order, count in sorted(pair_order_counts.items())
    }
    return first_query_ids, first_position_counts, order_distribution


def _finite_number(value: Any, *, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source} must be numeric")
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{source} must be finite and non-negative")
    return number


def _gold_documents(qrels: Mapping[str, Mapping[str, int]], query_id: str) -> set[str]:
    return {document_id for document_id, grade in qrels[query_id].items() if grade > 0}


def _mean(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


def _score_arm(
    rows: Mapping[str, Mapping[str, Any]],
    qrels: Mapping[str, Mapping[str, int]],
    *,
    arm: str,
    actual_characters_available: bool,
) -> dict[str, Any]:
    rankings = {}
    for query_id in qrels:
        rerank = _ranking(rows[query_id], "rerank", source=f"{arm} rerank for {query_id}")
        if len(rerank) > FINAL_TOP_K:
            raise ValueError(f"{arm} final ranking exceeds 20 chunks for {query_id}")
        rankings[query_id] = [
            _validate_hit(
                hit,
                source=f"{arm} rerank for {query_id}",
                require_score=True,
            )[1]
            for hit in rerank
        ]
    evaluation = evaluate_questions(qrels, rankings)

    per_query = {}
    for query_id in qrels:
        selection = _ranking(rows[query_id], "selection", source=f"{arm} selection for {query_id}")
        rerank = _ranking(rows[query_id], "rerank", source=f"{arm} rerank for {query_id}")
        selected_hits = [
            _validate_hit(
                hit,
                source=f"{arm} selection for {query_id}",
                require_score=True,
            )
            for hit in selection
        ]
        rerank_hits = [
            _validate_hit(
                hit,
                source=f"{arm} rerank for {query_id}",
                require_score=True,
            )
            for hit in rerank
        ]
        selected_chunks = [chunk_id for chunk_id, _, _ in selected_hits]
        selected_documents = {document_id for _, document_id, _ in selected_hits}
        final_chunks = [chunk_id for chunk_id, _, _ in rerank_hits]
        final_documents = {document_id for _, document_id, _ in rerank_hits}
        gold_documents = _gold_documents(qrels, query_id)
        oracle_found = selected_documents.intersection(gold_documents)

        raw_coverage = {}
        for cutoff in (5, 10, 20):
            raw_documents = {document_id for _, document_id, _ in rerank_hits[:cutoff]}
            raw_coverage[f"@{cutoff}_gold_coverage"] = (
                len(raw_documents.intersection(gold_documents)) / len(gold_documents)
                if gold_documents
                else None
            )

        question = evaluation.questions[query_id]
        output_characters = (
            rows[query_id]["output_characters"] if actual_characters_available else None
        )
        per_query[query_id] = {
            "evaluated": question.evaluated,
            "metrics": dict(question.metrics),
            "selection": {
                "actual_chunks": len(selected_chunks),
                "unique_chunks": len(set(selected_chunks)),
                "unique_documents": len(selected_documents),
                "duplicate_chunks": len(selected_chunks) - len(set(selected_chunks)),
                "duplicate_documents": len(selected_chunks) - len(selected_documents),
                "candidate_oracle_gold_found": len(oracle_found),
                "candidate_oracle_gold_total": len(gold_documents),
                "candidate_oracle_coverage": (
                    len(oracle_found) / len(gold_documents) if gold_documents else None
                ),
                "below_configured_budget": len(selected_chunks) < ARMS[arm]["rerank_top_k"],
            },
            "final_raw_chunks": {
                "count": len(rerank_hits),
                "unique_chunks": len(set(final_chunks)),
                "unique_documents": len(final_documents),
                "duplicate_chunks": len(final_chunks) - len(set(final_chunks)),
                "duplicate_documents": len(final_chunks) - len(final_documents),
                "gold_coverage": raw_coverage,
            },
            "cost": {
                "rerank_chunks": len(selected_chunks),
                "output_characters": output_characters,
            },
        }

    selection_rows = [item["selection"] for item in per_query.values()]
    oracle_values = [
        item["candidate_oracle_coverage"]
        for item in selection_rows
        if item["candidate_oracle_coverage"] is not None
    ]
    actually_selected = sum(item["actual_chunks"] for item in selection_rows)
    duplicate_chunks = sum(item["duplicate_chunks"] for item in selection_rows)
    duplicate_documents = sum(item["duplicate_documents"] for item in selection_rows)
    oracle_gold_found = sum(item["candidate_oracle_gold_found"] for item in selection_rows)
    oracle_gold_total = sum(item["candidate_oracle_gold_total"] for item in selection_rows)
    raw_values = {
        cutoff: [
            item["final_raw_chunks"]["gold_coverage"][f"@{cutoff}_gold_coverage"]
            for item in per_query.values()
            if item["final_raw_chunks"]["gold_coverage"][f"@{cutoff}_gold_coverage"] is not None
        ]
        for cutoff in (5, 10, 20)
    }
    output_values = [
        item["cost"]["output_characters"]
        for item in per_query.values()
        if item["cost"]["output_characters"] is not None
    ]
    final_rows = [item["final_raw_chunks"] for item in per_query.values()]
    final_chunk_count = sum(item["count"] for item in final_rows)
    final_duplicate_chunks = sum(item["duplicate_chunks"] for item in final_rows)
    final_duplicate_documents = sum(item["duplicate_documents"] for item in final_rows)
    return {
        "aggregate_metrics": dict(evaluation.metrics),
        "question_count": evaluation.question_count,
        "evaluated_count": evaluation.evaluated_count,
        "skipped_unjudged_count": evaluation.skipped_unjudged_count,
        "skipped_no_positive_count": evaluation.skipped_no_positive_count,
        "selection": {
            "query_count": len(per_query),
            "total_actual_chunks": actually_selected,
            "mean_actual_chunks": actually_selected / len(per_query),
            "mean_unique_documents": fmean(item["unique_documents"] for item in selection_rows),
            "duplicate_chunks": duplicate_chunks,
            "duplicate_chunk_rate": (
                duplicate_chunks / actually_selected if actually_selected else 0.0
            ),
            "duplicate_documents": duplicate_documents,
            "below_configured_budget_count": sum(
                item["below_configured_budget"] for item in selection_rows
            ),
            "candidate_oracle_coverage": _mean(oracle_values),
            "candidate_oracle_coverage_pooled": (
                oracle_gold_found / oracle_gold_total if oracle_gold_total else None
            ),
        },
        "final_raw_chunk_coverage": {
            "total_final_raw_chunks": final_chunk_count,
            "mean_final_raw_chunks": fmean(item["count"] for item in final_rows),
            "duplicate_chunks": final_duplicate_chunks,
            "duplicate_chunk_rate": (
                final_duplicate_chunks / final_chunk_count if final_chunk_count else 0.0
            ),
            "duplicate_documents": final_duplicate_documents,
            "final_raw_chunks@5_gold_coverage": _mean(raw_values[5]),
            "final_raw_chunks@10_gold_coverage": _mean(raw_values[10]),
            "final_raw_chunks@20_gold_coverage": _mean(raw_values[20]),
        },
        "cost": {
            "actual_rerank_chunks": actually_selected,
            "mean_actual_rerank_chunks": actually_selected / len(per_query),
            "retrieved_output_characters": sum(output_values) if output_values else None,
            "mean_retrieved_output_characters_per_query": _mean(output_values),
            "token_estimate": None,
            "dollar_estimate": None,
        },
        "per_query": per_query,
    }


def _paired_result(
    baseline: AggregateEvaluation,
    candidate: AggregateEvaluation,
    *,
    metric: str,
    confidence: float,
) -> dict[str, Any]:
    result = paired_bootstrap_comparison(
        baseline,
        candidate,
        metric=metric,
        sample_count=BOOTSTRAP_SAMPLES,
        confidence=confidence,
        seed=BOOTSTRAP_SEED,
    )
    payload = asdict(result)
    payload["excludes_zero"] = payload["ci_low"] > 0 or payload["ci_high"] < 0
    payload["interval"] = "paired percentile bootstrap over gold-overlap query clusters"
    return payload


def _comparisons(
    evaluations: Mapping[str, AggregateEvaluation],
) -> dict[str, Any]:
    baseline = evaluations["baseline"]
    output = {}
    for arm in ARM_NAMES[1:]:
        primary = _paired_result(
            baseline,
            evaluations[arm],
            metric=PRIMARY_METRIC,
            confidence=BONFERRONI_CONFIDENCE,
        )
        output[arm] = {
            "primary": {
                "metric": PRIMARY_METRIC,
                "multiplicity": "Bonferroni over 4 candidate arms versus baseline",
                **primary,
            },
            "exploratory": {
                metric: _paired_result(
                    baseline,
                    evaluations[arm],
                    metric=metric,
                    confidence=0.95,
                )
                for metric in EXPLORATORY_METRICS
            },
        }
    return output


def _select_arm(
    metrics_by_arm: Mapping[str, Mapping[str, float]],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    baseline = metrics_by_arm["baseline"]
    ndcg_margin = _rule_number(protocol, "ndcg10_noninferiority_margin")
    recall5_margin = _rule_number(protocol, "recall5_noninferiority_margin")
    minimum_gain = _rule_number(protocol, "minimum_ndcg10_or_recall20_gain")
    near_best_margin = _rule_number(protocol, "near_best_ndcg10_margin")
    candidates = []
    eligible = []
    for arm in ARM_NAMES:
        metrics = metrics_by_arm[arm]
        ndcg_delta = metrics[PRIMARY_METRIC] - baseline[PRIMARY_METRIC]
        recall5_delta = metrics["Recall@5"] - baseline["Recall@5"]
        recall20_delta = metrics["Recall@20"] - baseline["Recall@20"]
        qualifies = (
            arm != "baseline"
            and ndcg_delta >= -ndcg_margin
            and recall5_delta >= -recall5_margin
            and (ndcg_delta >= minimum_gain or recall20_delta >= minimum_gain)
        )
        candidate = {
            "arm": arm,
            "rerank_top_k": ARMS[arm]["rerank_top_k"],
            "ndcg10": metrics[PRIMARY_METRIC],
            "ndcg10_delta": ndcg_delta,
            "recall5": metrics["Recall@5"],
            "recall5_delta": recall5_delta,
            "recall20": metrics["Recall@20"],
            "recall20_delta": recall20_delta,
            "eligible": qualifies,
        }
        candidates.append(candidate)
        if qualifies:
            eligible.append(candidate)

    if not eligible:
        selected_arm = "baseline"
        best_ndcg = None
        near_best = []
        reason = "No non-baseline arm met the protocol eligibility rule; baseline was retained."
    else:
        best_ndcg = max(item["ndcg10"] for item in eligible)
        near_best = [item for item in eligible if best_ndcg - item["ndcg10"] <= near_best_margin]
        selected = min(
            near_best,
            key=lambda item: (
                item["rerank_top_k"],
                -item["ndcg10"],
                ARM_NAMES.index(item["arm"]),
            ),
        )
        selected_arm = selected["arm"]
        reason = (
            f"{selected_arm} met the eligibility thresholds and was within "
            f"{near_best_margin:g} nDCG@10 of the best eligible arm. Among near-best "
            f"arms, it had the smallest rerank budget (or the higher nDCG@10 when "
            f"rerank budgets tied)."
        )
    return {
        "rule": dict(protocol["selection_rule"]),
        "best_eligible_ndcg10": best_ndcg,
        "near_best_arms": [item["arm"] for item in near_best],
        "candidates": candidates,
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
        "protocol": protocol,
        "selected_arm": selection["selected_arm"],
        "reason": selection["reason"],
        "selection": dict(selection),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    if path.exists():
        existing = _read_json(path)
        comparable = ("protocol", "selected_arm", "reason", "selection")
        if any(existing.get(key) != payload.get(key) for key in comparable):
            raise ValueError("existing frozen-selection.json differs from this DEV screen")
        return path
    dump(path, payload)
    return path


def screen(
    input_dir: Path,
    protocol_path: Path,
    data_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Score the DEV superset, select and freeze one arm, and return the report."""

    protocol = _read_json(protocol_path)
    _validate_protocol(protocol)
    metadata = _read_json(input_dir / "screen-superset.meta.json")
    _validate_screen_metadata(metadata, protocol)
    source = _load_official_split(
        "train",
        data_path,
        input_dir=input_dir,
        protocol_path=protocol_path,
        frozen_path=None,
    )
    trace_path = input_dir / "screen-superset.jsonl"
    rows = _rows_by_query(_read_jsonl(trace_path), trace_path)
    _validate_complete_split(
        rows,
        source.qrels,
        declared_count=metadata["queries"],
        source=trace_path.name,
        expected_count=EXPECTED_QUERY_COUNTS["train"],
    )
    _validate_screen_order(rows, protocol, source.qrels)

    replayed = {}
    for arm in ARM_NAMES:
        replayed[arm] = {}
        for query_id in source.qrels:
            _validate_superset_row(rows[query_id], query_id)
            try:
                replayed[arm][query_id] = replay_arm(
                    rows[query_id],
                    ARMS[arm],
                    final_top_k=FINAL_TOP_K,
                )
            except ValueError as exc:
                raise ValueError(f"{arm} cannot be replayed for {query_id}: {exc}") from exc

    arm_reports = {
        arm: _score_arm(
            replayed[arm],
            source.qrels,
            arm=arm,
            actual_characters_available=False,
        )
        for arm in ARM_NAMES
    }
    evaluations = {}
    for arm in ARM_NAMES:
        rankings = {
            query_id: [
                hit["document_id"]
                for hit in _ranking(
                    replayed[arm][query_id],
                    "rerank",
                    source=f"{arm} replay for {query_id}",
                )[:FINAL_TOP_K]
            ]
            for query_id in source.qrels
        }
        evaluations[arm] = evaluate_questions(source.qrels, rankings)

    comparisons = _comparisons(evaluations)
    selection = _select_arm(
        {arm: arm_reports[arm]["aggregate_metrics"] for arm in ARM_NAMES},
        protocol,
    )
    frozen_path = _freeze_selection(input_dir, protocol, selection)
    source_output_characters = [
        row["output_characters"]
        for row in rows.values()
        if isinstance(row.get("output_characters"), int)
        and not isinstance(row["output_characters"], bool)
    ]
    report = {
        "schema_version": 1,
        "phase": "screen",
        "scope": {
            "split": "train",
            "query_count": EXPECTED_QUERY_COUNTS["train"],
            "quality_only": True,
            "timing_reported": False,
            "test_exposed": False,
            "retrieval_only": True,
            "generation_evaluated": False,
            "context_budget_token_admission_evaluated": False,
            "blind_holdout_claim": False,
        },
        "protocol_seed": protocol["seed"],
        "protocol_declared_at": protocol.get("declared_at"),
        "metric_definitions": METRIC_DEFINITIONS,
        "query_alignment": {
            "official_train_queries": EXPECTED_QUERY_COUNTS["train"],
            "unique_trace_queries": len(rows),
            "missing": [],
            "duplicate": [],
            "metadata_matched": True,
        },
        "source_superset_output_characters": (
            sum(source_output_characters) if len(source_output_characters) == len(rows) else None
        ),
        "source_superset_note": (
            "The source run emitted 60 chunks. Per-arm output characters are not "
            "assignable from this replay and are therefore not reported."
        ),
        "arms": arm_reports,
        "comparisons": comparisons,
        "selection": {
            **selection,
            "frozen_selection": frozen_path.name,
        },
    }
    return report, frozen_path


def _latency_summary(values: Mapping[str, float]) -> dict[str, Any]:
    ordered = list(values.values())
    return {
        "n": len(ordered),
        "mean_ms": fmean(ordered) if ordered else None,
        "p50_ms": quantile(ordered, 0.5),
        "p95_ms": quantile(ordered, 0.95),
    }


def _timing_delta(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    groups: Mapping[str, str],
) -> dict[str, Any]:
    if not baseline:
        return {
            "n": 0,
            "estimate_ms": None,
            "ci95_low_ms": None,
            "ci95_high_ms": None,
            "groups": 0,
        }
    result = grouped_difference(
        dict(baseline),
        dict(candidate),
        dict(groups),
        samples=BOOTSTRAP_SAMPLES,
        seed=BOOTSTRAP_SEED,
    )
    result["n"] = len(baseline)
    return result


def _order_strata(
    rows: Mapping[str, Mapping[str, dict[str, Any]]],
    warm_queries: set[str],
    qrels: Mapping[str, Mapping[str, int]],
    groups: Mapping[str, str],
    *,
    basis: str,
    getter,
) -> dict[str, Any]:
    baseline_total = {query_id: getter(rows["baseline"][query_id]) for query_id in warm_queries}
    output = {}
    for arm in ARM_NAMES[1:]:
        candidate_total = {query_id: getter(rows[arm][query_id]) for query_id in warm_queries}
        by_position = {}
        for position in range(1, len(ARM_NAMES) + 1):
            subset = {
                query_id
                for query_id in warm_queries
                if rows[arm][query_id]["pair_order"].index(arm) + 1 == position
            }
            by_position[str(position)] = _timing_delta(
                {query_id: baseline_total[query_id] for query_id in subset},
                {query_id: candidate_total[query_id] for query_id in subset},
                {query_id: groups[query_id] for query_id in subset},
            )

        before_first = {
            query_id
            for query_id in warm_queries
            if rows[arm][query_id]["pair_order"].index(arm)
            < rows[arm][query_id]["pair_order"].index("baseline")
        }
        after_first = warm_queries - before_first
        output[arm] = {
            "basis": basis,
            "sign": "candidate minus baseline; negative is faster",
            "by_candidate_position": by_position,
            "relative_to_baseline": {
                "before": _timing_delta(
                    {query_id: baseline_total[query_id] for query_id in before_first},
                    {query_id: candidate_total[query_id] for query_id in before_first},
                    {query_id: groups[query_id] for query_id in before_first},
                ),
                "after": _timing_delta(
                    {query_id: baseline_total[query_id] for query_id in after_first},
                    {query_id: candidate_total[query_id] for query_id in after_first},
                    {query_id: groups[query_id] for query_id in after_first},
                ),
            },
        }
    return output


def _timing_report(
    rows: Mapping[str, Mapping[str, dict[str, Any]]],
    qrels: Mapping[str, Mapping[str, int]],
    first_query_ids: set[str],
    first_position_counts: Mapping[str, int],
    order_distribution: Mapping[str, int],
) -> dict[str, Any]:
    warm_queries = set(qrels) - first_query_ids
    groups = query_groups(dict(qrels))
    totals = {
        arm: {
            query_id: _finite_number(
                rows[arm][query_id]["total_ms"],
                source=f"{arm} total_ms for {query_id}",
            )
            for query_id in warm_queries
        }
        for arm in ARM_NAMES
    }
    rerank = {
        arm: {
            query_id: _finite_number(
                rows[arm][query_id]["stages"]["rerank"]["latency_ms"],
                source=f"{arm} rerank latency for {query_id}",
            )
            for query_id in warm_queries
        }
        for arm in ARM_NAMES
    }
    arm_timings = {}
    for arm in ARM_NAMES:
        arm_timings[arm] = {
            "total_ms": _latency_summary(totals[arm]),
            "rerank_ms": _latency_summary(rerank[arm]),
            "total_delta_vs_baseline_ms": _timing_delta(
                totals["baseline"],
                totals[arm],
                {query_id: groups[query_id] for query_id in warm_queries},
            ),
            "rerank_delta_vs_baseline_ms": _timing_delta(
                rerank["baseline"],
                rerank[arm],
                {query_id: groups[query_id] for query_id in warm_queries},
            ),
        }
    return {
        "policy": "Exclude ordinal-0 query from all arms; use only observed timings.",
        "excluded_ordinal_zero_queries": sorted(first_query_ids),
        "warm_query_count": len(warm_queries),
        "first_position_counts": dict(first_position_counts),
        "pair_order_distribution": dict(order_distribution),
        "arms": arm_timings,
        "order_strata": {
            "total_ms": _order_strata(
                rows,
                warm_queries,
                qrels,
                groups,
                basis="total_ms",
                getter=lambda row: float(row["total_ms"]),
            ),
            "rerank_ms": _order_strata(
                rows,
                warm_queries,
                qrels,
                groups,
                basis="rerank_ms",
                getter=lambda row: float(row["stages"]["rerank"]["latency_ms"]),
            ),
        },
    }


def regression(
    input_dir: Path,
    protocol_path: Path,
    frozen_path: Path,
    data_path: Path | None = None,
) -> dict[str, Any]:
    """Report the exposed-test interleaved regression from a frozen selection."""

    protocol = _read_json(protocol_path)
    _validate_protocol(protocol)
    frozen = _read_json(frozen_path)
    if frozen.get("protocol") != protocol:
        raise ValueError("frozen selection protocol differs")
    if frozen.get("selected_arm") not in ARM_NAMES:
        raise ValueError("frozen selection does not name an implemented arm")

    metadata_by_arm = {
        arm: _read_json(input_dir / f"regression-{arm}.meta.json") for arm in ARM_NAMES
    }
    identity = _validate_common_metadata(metadata_by_arm, protocol)
    source = _load_official_split(
        "test",
        data_path,
        input_dir=input_dir,
        protocol_path=protocol_path,
        frozen_path=frozen_path,
    )
    rows_by_arm = {}
    for arm in ARM_NAMES:
        path = input_dir / f"regression-{arm}.jsonl"
        rows = _rows_by_query(_read_jsonl(path), path)
        _validate_complete_split(
            rows,
            source.qrels,
            declared_count=metadata_by_arm[arm]["queries"],
            source=path.name,
            expected_count=EXPECTED_QUERY_COUNTS["test"],
        )
        for query_id in source.qrels:
            _validate_regression_row(rows[query_id], arm=arm, query_id=query_id)
        rows_by_arm[arm] = rows

    first_query_ids, first_position_counts, order_distribution = _validate_regression_order(
        rows_by_arm,
        protocol,
        source.qrels,
    )
    arm_reports = {
        arm: _score_arm(
            rows_by_arm[arm],
            source.qrels,
            arm=arm,
            actual_characters_available=True,
        )
        for arm in ARM_NAMES
    }
    rankings = {}
    for arm in ARM_NAMES:
        rankings[arm] = {
            query_id: [
                hit["document_id"]
                for hit in _ranking(
                    rows_by_arm[arm][query_id],
                    "rerank",
                    source=f"{arm} rerank for {query_id}",
                )[:FINAL_TOP_K]
            ]
            for query_id in source.qrels
        }
    evaluations = {arm: evaluate_questions(source.qrels, rankings[arm]) for arm in ARM_NAMES}
    comparisons = _comparisons(evaluations)
    timing = _timing_report(
        rows_by_arm,
        source.qrels,
        first_query_ids,
        first_position_counts,
        order_distribution,
    )
    return {
        "schema_version": 1,
        "phase": "regression",
        "scope": {
            "split": "test",
            "query_count": EXPECTED_QUERY_COUNTS["test"],
            "test_exposed": True,
            "blind_holdout_claim": False,
            "regression_only": True,
            "retrieval_only": True,
            "generation_evaluated": False,
            "context_budget_token_admission_evaluated": False,
            "timing_basis": "Actual five-arm randomized interleaving, concurrency not inferred.",
        },
        "protocol": protocol,
        "frozen_selection": frozen,
        "selected_arm": frozen["selected_arm"],
        "selection_source": "frozen DEV selection; no regression reselection",
        "experiment_identity": identity,
        "metric_definitions": METRIC_DEFINITIONS,
        "query_alignment": {
            "official_test_queries": EXPECTED_QUERY_COUNTS["test"],
            "unique_queries_per_arm": len(source.qrels),
            "missing": [],
            "duplicate": [],
            "metadata_matched": True,
        },
        "arms": arm_reports,
        "comparisons": comparisons,
        "timing": timing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("screen", "regression"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()

    if args.phase == "screen":
        if args.frozen is not None:
            parser.error("screen writes input/frozen-selection.json; do not pass --frozen")
        report, _ = screen(args.input, args.protocol, args.data)
    else:
        frozen_path = args.frozen or args.input / "frozen-selection.json"
        report = regression(args.input, args.protocol, frozen_path, args.data)
    dump(args.output, report)
    print(args.output)


if __name__ == "__main__":
    main()
