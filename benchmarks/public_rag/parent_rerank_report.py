"""Score the bounded child-retrieval, parent-context reranking ablation."""

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

from benchmarks.public_rag.data import load_scifact, query_groups
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.scifact_report import quantile
from benchmarks.public_rag.scifact_runner import DATA, dump
from benchmarks.public_rag.scoring import (
    AggregateEvaluation,
    evaluate_questions,
    paired_bootstrap_comparison,
)

ARMS = ("baseline", "parent")
STAGES = ("dense", "sparse", "fusion", "selection", "rerank")
CAPS = {"dense": 30, "sparse": 30, "fusion": 20, "selection": 20, "rerank": 20}
COMPUTE = (
    "input_chunks",
    "neural_pairs",
    "input_characters",
    "expanded_parent_count",
    "encoded_tokens",
    "padded_tokens",
    "truncated_pairs",
    "max_untruncated_tokens",
)
COUNTS = {"train": 809, "test": 300}
PRIMARY = "nDCG@10"
SECONDARY = ("Recall@5", "Recall@20", "MRR@10")
SAMPLES = 10_000
TOLERANCE = 1e-6


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read trace: {path}") from exc
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"{path.name} line {number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {number} must be a JSON object")
        rows.append(row)
    return rows


def _finite(value: object, name: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not isfinite(number) or (nonnegative and number < 0):
        raise ValueError(f"{name} must be finite" + (" and non-negative" if nonnegative else ""))
    return number


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    if protocol.get("arms") != {"baseline": "chunk", "parent": "parent"}:
        raise ValueError("protocol arms must be baseline=chunk and parent=parent")
    for key, cap in (
        ("dense_top_k", 30),
        ("sparse_top_k", 30),
        ("candidate_top_k", 20),
        ("rerank_top_k", 20),
        ("final_top_k", 20),
    ):
        if protocol.get(key) != cap:
            raise ValueError(f"protocol {key} must be {cap}")
    seed = protocol.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("protocol seed must be an integer")
    if not isinstance(protocol.get("snapshot_id"), str) or not protocol["snapshot_id"]:
        raise ValueError("protocol snapshot_id must be non-empty")
    rule = protocol.get("selection_rule")
    if not isinstance(rule, Mapping):
        raise ValueError("protocol requires selection_rule")
    for key in ("min_ndcg10_gain", "max_recall5_loss", "max_recall20_loss"):
        _finite(rule.get(key), f"selection_rule.{key}")
    if not isinstance(rule.get("require_primary_ci_above_zero"), bool):
        raise ValueError("selection_rule.require_primary_ci_above_zero must be boolean")


def _validate_meta(
    meta: Mapping[str, Any],
    *,
    arm: str,
    phase: str,
    count: int,
    protocol: Mapping[str, Any],
) -> None:
    split = "train" if phase == "dev" else "test"
    for key, value in {
        "protocol": dict(protocol),
        "arm": arm,
        "phase": phase,
        "split": split,
        "queries": count,
        "snapshot_id": protocol["snapshot_id"],
    }.items():
        if meta.get(key) != value:
            raise ValueError(f"{arm} metadata has inconsistent {key}")
    if not isinstance(meta.get("retrieval_identity"), Mapping):
        raise ValueError(f"{arm} metadata requires retrieval_identity")
    if meta.get("paired_interleaved") is not True:
        raise ValueError(f"{arm} metadata must declare paired_interleaved=true")
    if meta.get("git_dirty") is not True:
        raise ValueError(f"{arm} metadata must declare git_dirty=true")
    cold = meta.get("cold_load_ms")
    values = cold if isinstance(cold, Mapping) else {"total": cold}
    if not values:
        raise ValueError(f"{arm} cold_load_ms must be present")
    for name, value in values.items():
        _finite(value, f"{arm} cold_load_ms.{name}")


def _ranking(row: Mapping[str, Any], stage: str) -> list[Mapping[str, Any]]:
    stages = row.get("stages")
    value = stages.get(stage) if isinstance(stages, Mapping) else None
    ranking = value.get("ranking") if isinstance(value, Mapping) else None
    if not isinstance(ranking, list):
        raise ValueError(f"{row.get('query_id')} requires stages.{stage}.ranking")
    return ranking


def _hit(
    value: object,
    *,
    source: str,
    documents: set[str] | None,
) -> tuple[str, str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{source} hit must be an object")
    chunk, document = value.get("chunk_id"), value.get("document_id")
    if not isinstance(chunk, str) or not chunk:
        raise ValueError(f"{source} has invalid chunk_id")
    if not isinstance(document, str) or not document:
        raise ValueError(f"{source} has invalid document_id")
    if documents is not None and document not in documents:
        raise ValueError(f"{source} maps to unknown source document {document}")
    return chunk, document, _finite(value.get("score"), f"{source} score", nonnegative=False)


def _db(row: Mapping[str, Any], name: str) -> float:
    values = row.get("db_ms")
    aliases = {"scope": ("scope", "snapshot_scope"), "load": ("load", "chunk_load")}[name]
    present = [key for key in aliases if isinstance(values, Mapping) and key in values]
    if len(present) != 1:
        raise ValueError(f"{row.get('query_id')} requires one db_ms.{name} value")
    return _finite(values[present[0]], f"db_ms.{name}")


def _validate_compute(
    value: object,
    *,
    arm: str,
    query: str,
    selection: Sequence[Mapping[str, Any]],
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{arm} {query} requires compute")
    counts = {key: _count(value.get(key), f"{arm} {query} {key}") for key in COMPUTE}
    pairs = len(selection) if arm == "baseline" else len({hit["document_id"] for hit in selection})
    if counts["input_chunks"] != len(selection) or counts["neural_pairs"] != pairs:
        raise ValueError(f"{arm} {query} neural input disagrees with selection")
    if arm == "baseline" and counts["expanded_parent_count"] != 0:
        raise ValueError(f"{arm} {query} parent expansion must be zero")
    if counts["encoded_tokens"] < counts["neural_pairs"]:
        raise ValueError(f"{arm} {query} encoded_tokens is less than neural_pairs")
    if counts["padded_tokens"] < counts["encoded_tokens"]:
        raise ValueError(f"{arm} {query} padded_tokens must be at least encoded_tokens")
    if counts["truncated_pairs"] > counts["neural_pairs"]:
        raise ValueError(f"{arm} {query} truncated_pairs exceeds neural_pairs")
    if counts["expanded_parent_count"] > counts["neural_pairs"]:
        raise ValueError(f"{arm} {query} expanded_parent_count exceeds neural_pairs")
    if counts["neural_pairs"] and (
        counts["input_characters"] == 0 or counts["max_untruncated_tokens"] == 0
    ):
        raise ValueError(f"{arm} {query} has zero cost for nonempty neural input")


def _validate_row(
    row: Mapping[str, Any],
    *,
    arm: str,
    query: str,
    documents: set[str] | None,
) -> None:
    if row.get("query_id") != query:
        raise ValueError(f"{arm} row query does not match {query}")
    ordinal = row.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError(f"{arm} {query} has invalid ordinal")
    if not isinstance(row.get("first_inference"), bool) or row["first_inference"] != (ordinal == 0):
        raise ValueError(f"{arm} {query} first_inference disagrees with ordinal")
    order = row.get("pair_order")
    if not isinstance(order, list) or len(order) != 2 or set(order) != set(ARMS):
        raise ValueError(f"{arm} {query} pair_order is not an arm permutation")
    _finite(row.get("total_ms"), f"{arm} {query} total_ms")
    _db(row, "scope")
    _db(row, "load")
    stages = row.get("stages")
    if not isinstance(stages, Mapping):
        raise ValueError(f"{arm} {query} requires stages")
    mapping: dict[str, str] = {}
    for stage in STAGES:
        stage_value = stages.get(stage)
        if not isinstance(stage_value, Mapping):
            raise ValueError(f"{arm} {query} requires stages.{stage}")
        _finite(stage_value.get("latency_ms"), f"{arm} {query} {stage} latency")
        ranking = _ranking(row, stage)
        if len(ranking) > CAPS[stage]:
            raise ValueError(f"{arm} {query} {stage} exceeds its cap")
        if stage in ("fusion", "selection", "rerank") and len(ranking) != 20:
            raise ValueError(f"{arm} {query} {stage} must expose all 20 raw chunks")
        if len({hit.get("chunk_id") for hit in ranking}) != len(ranking):
            raise ValueError(f"{arm} {query} {stage} contains duplicate chunks")
        for hit in ranking:
            chunk, document, _ = _hit(hit, source=f"{arm} {query} {stage}", documents=documents)
            if mapping.setdefault(chunk, document) != document:
                raise ValueError(f"{arm} {query} chunk-to-document mapping differs")
    fusion = {hit["chunk_id"] for hit in _ranking(row, "fusion")}
    selection = _ranking(row, "selection")
    final = _ranking(row, "rerank")
    if not {hit["chunk_id"] for hit in selection}.issubset(fusion):
        raise ValueError(f"{arm} {query} selection is not a fusion subset")
    if {hit["chunk_id"] for hit in final} != {hit["chunk_id"] for hit in selection}:
        raise ValueError(f"{arm} {query} rerank candidates differ from selection")
    fusion_scores = {hit["chunk_id"]: hit["score"] for hit in _ranking(row, "fusion")}
    expected = sorted(
        final, key=lambda hit: (-hit["score"], -fusion_scores[hit["chunk_id"]], hit["chunk_id"])
    )
    if [hit["chunk_id"] for hit in final] != [hit["chunk_id"] for hit in expected]:
        raise ValueError(f"{arm} {query} final order differs from the measured production sort")
    if arm == "parent":
        scores: dict[str, float] = {}
        for hit in final:
            prior = scores.setdefault(hit["document_id"], hit["score"])
            if abs(prior - hit["score"]) > TOLERANCE:
                raise ValueError(f"{query} parent score was not broadcast to every child")
    _validate_compute(row.get("compute"), arm=arm, query=query, selection=selection)


def _same_pre_rerank(left: Mapping[str, Any], right: Mapping[str, Any], query: str) -> None:
    for stage in ("dense", "sparse", "fusion", "selection"):
        first, second = _ranking(left, stage), _ranking(right, stage)
        if len(first) != len(second):
            raise ValueError(f"{query} {stage} differs across arms")
        for a, b in zip(first, second, strict=True):
            if (
                a["chunk_id"] != b["chunk_id"]
                or a["document_id"] != b["document_id"]
                or abs(a["score"] - b["score"]) > TOLERANCE
            ):
                raise ValueError(f"{query} {stage} pre-rerank trace differs")


def _expected_order(
    protocol: Mapping[str, Any], qrels: Mapping[str, Mapping[str, int]]
) -> dict[str, tuple[int, tuple[str, ...]]]:
    queries = sorted(qrels)
    Random(protocol["seed"]).shuffle(queries)
    rng = Random(protocol["seed"] + 23)
    output = {}
    for ordinal, query in enumerate(queries):
        order = list(ARMS)
        rng.shuffle(order)
        output[query] = ordinal, tuple(order)
    return output


def _validate_captures(
    captures: Mapping[str, tuple[Mapping[str, Any], list[dict[str, Any]]]],
    *,
    protocol: Mapping[str, Any],
    phase: str,
    qrels: Mapping[str, Mapping[str, int]],
    expected_count: int,
    documents: set[str] | None,
) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]]]:
    for arm in ARMS:
        _validate_meta(
            captures[arm][0], arm=arm, phase=phase, count=expected_count, protocol=protocol
        )
    baseline_meta = captures["baseline"][0]
    for arm in ARMS[1:]:
        for key in ("protocol", "snapshot_id", "retrieval_identity", "cold_load_ms"):
            if captures[arm][0].get(key) != baseline_meta.get(key):
                raise ValueError(f"metadata differs across arms: {key}")

    rows: dict[str, dict[str, dict[str, Any]]] = {}
    ids: dict[str, list[str]] = {}
    for arm in ARMS:
        indexed = {}
        order = []
        for row in captures[arm][1]:
            query = row.get("query_id")
            if not isinstance(query, str) or not query:
                raise ValueError(f"{arm} has invalid query_id")
            if query in indexed:
                raise ValueError(f"duplicate query ID in {arm}: {query}")
            indexed[query] = row
            order.append(query)
        rows[arm] = indexed
        ids[arm] = order
    if ids["baseline"] != ids["parent"]:
        raise ValueError("arms must contain the same query IDs in the same order")
    if len(qrels) != expected_count or len(ids["baseline"]) != expected_count:
        raise ValueError("trace and qrels must cover the complete declared split")
    expected = _expected_order(protocol, qrels)
    if ids["baseline"] != list(expected):
        raise ValueError("query order differs from the seeded protocol order")

    first_counts: Counter[str] = Counter()
    for query in ids["baseline"]:
        baseline, parent = rows["baseline"][query], rows["parent"][query]
        _validate_row(baseline, arm="baseline", query=query, documents=documents)
        _validate_row(parent, arm="parent", query=query, documents=documents)
        ordinal, order = expected[query]
        if baseline["ordinal"] != ordinal or baseline["pair_order"] != list(order):
            raise ValueError(f"{query} ordinal or randomized pair order differs from protocol")
        if parent["ordinal"] != ordinal or parent["pair_order"] != list(order):
            raise ValueError(f"{query} arms disagree on ordinal or pair order")
        _same_pre_rerank(baseline, parent, query)
        if {hit["chunk_id"] for hit in _ranking(baseline, "rerank")} != {
            hit["chunk_id"] for hit in _ranking(parent, "rerank")
        }:
            raise ValueError(f"{query} rerank child set differs across arms")
        first_counts[order[0]] += 1
    if expected_count > 1 and set(first_counts) != set(ARMS):
        raise ValueError("pair_order does not use both randomized arm positions")
    return ids["baseline"], rows


def _compute_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = {key: sum(row["compute"][key] for row in rows) for key in COMPUTE}
    totals["max_untruncated_tokens"] = max(
        (row["compute"]["max_untruncated_tokens"] for row in rows), default=0
    )
    n = len(rows)
    padding = totals["padded_tokens"] - totals["encoded_tokens"]
    return {
        **totals,
        "padding_slots": padding,
        "padding_overhead_rate": (
            padding / totals["encoded_tokens"] if totals["encoded_tokens"] else 0.0
        ),
        "truncation_rate": (
            totals["truncated_pairs"] / totals["neural_pairs"] if totals["neural_pairs"] else 0.0
        ),
        "mean_neural_pairs_per_query": totals["neural_pairs"] / n if n else 0.0,
        "mean_input_characters_per_query": totals["input_characters"] / n if n else 0.0,
        "mean_encoded_tokens_per_query": totals["encoded_tokens"] / n if n else 0.0,
        "mean_padded_tokens_per_query": totals["padded_tokens"] / n if n else 0.0,
    }


def _score_arm(
    rows: Mapping[str, Mapping[str, Any]], qrels: Mapping[str, Mapping[str, int]]
) -> tuple[dict[str, Any], AggregateEvaluation]:
    rankings = {
        query: tuple(hit["document_id"] for hit in _ranking(row, "rerank")[:20])
        for query, row in rows.items()
    }
    evaluation = evaluate_questions(qrels, rankings)
    return {
        "metrics": dict(evaluation.metrics),
        "evaluated_count": evaluation.evaluated_count,
        "skipped_unjudged_count": evaluation.skipped_unjudged_count,
        "skipped_no_positive_count": evaluation.skipped_no_positive_count,
        "missing_gold_documents": evaluation.missing_document_count,
        "gold_coverage": evaluation.gold_coverage,
        "compute": _compute_summary(list(rows.values())),
        "query_metrics": {
            query: dict(result.metrics) for query, result in evaluation.questions.items()
        },
    }, evaluation


def _paired(
    baseline: AggregateEvaluation,
    parent: AggregateEvaluation,
    *,
    metric: str,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    result = paired_bootstrap_comparison(
        baseline,
        parent,
        metric=metric,
        sample_count=samples,
        confidence=0.95,
        seed=seed,
    )
    return {
        **asdict(result),
        "metric": metric,
        "direction": "parent minus baseline",
        "interval": "paired percentile bootstrap over gold-overlap query clusters",
        "inference_valid": result.group_count >= 2,
    }


def _summary(values: Mapping[str, float]) -> dict[str, Any]:
    ordered = list(values.values())
    return {
        "n": len(ordered),
        "mean_ms": fmean(ordered) if ordered else None,
        "p50_ms": quantile(ordered, 0.5),
        "p95_ms": quantile(ordered, 0.95),
    }


def _timing_delta(
    baseline: Mapping[str, float],
    parent: Mapping[str, float],
    groups: Mapping[str, str],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    if not baseline:
        return {"baseline": _summary(baseline), "parent": _summary(parent), "difference": None}
    return {
        "baseline": _summary(baseline),
        "parent": _summary(parent),
        "difference": grouped_difference(
            dict(baseline), dict(parent), dict(groups), samples=samples, seed=seed
        ),
    }


def _timing(
    rows: Mapping[str, Mapping[str, Mapping[str, Any]]],
    qrels: Mapping[str, Mapping[str, int]],
    order: Sequence[str],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    warm = [query for query in order if rows["baseline"][query]["ordinal"] != 0]
    groups = query_groups(dict(qrels))
    warm_groups = {query: groups[query] for query in warm}

    def values(arm: str, getter) -> dict[str, float]:
        return {query: getter(rows[arm][query]) for query in warm}

    def stage_value(name: str):
        return lambda row: _finite(row["stages"][name]["latency_ms"], f"{name} latency")

    total_base = values("baseline", lambda row: _finite(row["total_ms"], "total_ms"))
    total_parent = values("parent", lambda row: _finite(row["total_ms"], "total_ms"))
    output = {
        "policy": (
            "Exclude ordinal 0 from both arms; timings cover only the two retriever calls. "
            "Token audits run after both calls and are excluded from latency."
        ),
        "warm_query_count": len(warm),
        "excluded_query_ids": [query for query in order if query not in warm],
        "total_ms": _timing_delta(
            total_base, total_parent, warm_groups, samples=samples, seed=seed
        ),
        "stages": {
            name: _timing_delta(
                values("baseline", stage_value(name)),
                values("parent", stage_value(name)),
                warm_groups,
                samples=samples,
                seed=seed,
            )
            for name in STAGES
        },
        "db_ms": {
            "scope_ms": _timing_delta(
                values("baseline", lambda row: _db(row, "scope")),
                values("parent", lambda row: _db(row, "scope")),
                warm_groups,
                samples=samples,
                seed=seed,
            ),
            "load_ms": _timing_delta(
                values("baseline", lambda row: _db(row, "load")),
                values("parent", lambda row: _db(row, "load")),
                warm_groups,
                samples=samples,
                seed=seed,
            ),
        },
        "order_strata": {},
    }
    for first in ARMS:
        subset = [query for query in warm if rows["parent"][query]["pair_order"][0] == first]
        subset_groups = {query: groups[query] for query in subset}
        output["order_strata"][f"{first}_first"] = {
            "n": len(subset),
            "total_ms": _timing_delta(
                {query: total_base[query] for query in subset},
                {query: total_parent[query] for query in subset},
                subset_groups,
                samples=samples,
                seed=seed,
            ),
            "rerank_ms": _timing_delta(
                {
                    query: _finite(
                        rows["baseline"][query]["stages"]["rerank"]["latency_ms"],
                        "rerank latency",
                    )
                    for query in subset
                },
                {
                    query: _finite(
                        rows["parent"][query]["stages"]["rerank"]["latency_ms"],
                        "rerank latency",
                    )
                    for query in subset
                },
                subset_groups,
                samples=samples,
                seed=seed,
            ),
        }
    return output


def _select(
    protocol: Mapping[str, Any],
    metrics: Mapping[str, Mapping[str, float]],
    primary: Mapping[str, Any],
) -> dict[str, Any]:
    rule = protocol["selection_rule"]
    base, parent = metrics["baseline"], metrics["parent"]
    observed = {
        "min_ndcg10_gain": parent[PRIMARY] - base[PRIMARY],
        "max_recall5_loss": base["Recall@5"] - parent["Recall@5"],
        "max_recall20_loss": base["Recall@20"] - parent["Recall@20"],
        "require_primary_ci_above_zero": primary["ci_low"],
    }
    gates = {
        "min_ndcg10_gain": {
            "observed": observed["min_ndcg10_gain"],
            "threshold": float(rule["min_ndcg10_gain"]),
            "passed": observed["min_ndcg10_gain"] >= float(rule["min_ndcg10_gain"]),
        },
        "max_recall5_loss": {
            "observed": observed["max_recall5_loss"],
            "threshold": float(rule["max_recall5_loss"]),
            "passed": observed["max_recall5_loss"] <= float(rule["max_recall5_loss"]),
        },
        "max_recall20_loss": {
            "observed": observed["max_recall20_loss"],
            "threshold": float(rule["max_recall20_loss"]),
            "passed": observed["max_recall20_loss"] <= float(rule["max_recall20_loss"]),
        },
        "require_primary_ci_above_zero": {
            "observed": primary["ci_low"],
            "required": rule["require_primary_ci_above_zero"],
            "inference_valid": primary["inference_valid"],
            "passed": (
                not rule["require_primary_ci_above_zero"]
                or (primary["inference_valid"] and primary["ci_low"] > 0)
            ),
        },
    }
    passed = all(gate["passed"] for gate in gates.values())
    return {
        "selected_arm": "parent" if passed else "baseline",
        "all_gates_passed": passed,
        "gates": gates,
    }


def _freeze(input_dir: Path, protocol: Mapping[str, Any], selection: Mapping[str, Any]) -> Path:
    path = input_dir / "frozen-selection.json"
    payload = {
        "protocol": dict(protocol),
        "selected_arm": selection["selected_arm"],
        "gates": dict(selection["gates"]),
    }
    if path.exists():
        existing = _json(path)
        if any(existing.get(key) != value for key, value in payload.items()):
            raise ValueError("existing frozen-selection.json differs")
        return path
    dump(path, payload)
    return path


def _validate_frozen(frozen: Mapping[str, Any], protocol: Mapping[str, Any]) -> None:
    if frozen.get("protocol") != dict(protocol):
        raise ValueError("frozen selection protocol differs")
    if frozen.get("selected_arm") != "parent":
        raise ValueError("regression requires frozen parent selection")
    if not isinstance(frozen.get("gates"), Mapping):
        raise ValueError("frozen selection requires gates")


def _label(input_dir: Path, phase: str, protocol: Path, frozen: Path | None) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "event": (
            "PARENT_DEV_LABEL_ACCESS"
            if phase == "dev"
            else "PARENT_EXPOSED_TEST_REGRESSION_LABEL_ACCESS"
        ),
        "at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "split": "train" if phase == "dev" else "test",
        "protocol": str(protocol),
    }
    if frozen:
        entry["frozen_selection"] = str(frozen)
    with (input_dir / "label-access.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def score_captures(
    input_dir: Path,
    *,
    protocol: Mapping[str, Any],
    phase: str,
    source: Any,
    expected_count: int | None = None,
    frozen: Mapping[str, Any] | None = None,
    samples: int = SAMPLES,
) -> dict[str, Any]:
    """Validate and score captured runs; tests may inject a small dataset."""

    _validate_protocol(protocol)
    if phase not in ("dev", "regression") or samples < 1:
        raise ValueError("phase must be dev/regression and samples must be positive")
    count = len(source.qrels) if expected_count is None else expected_count
    if set(source.queries) != set(source.qrels):
        raise ValueError("dataset queries and qrels have different IDs")
    documents = set(source.corpus) if isinstance(getattr(source, "corpus", None), Mapping) else None
    captures = {
        arm: (
            _json(input_dir / f"{phase}-{arm}.meta.json"),
            _jsonl(input_dir / f"{phase}-{arm}.jsonl"),
        )
        for arm in ARMS
    }
    order, rows = _validate_captures(
        captures,
        protocol=protocol,
        phase=phase,
        qrels=source.qrels,
        expected_count=count,
        documents=documents,
    )
    reports: dict[str, dict[str, Any]] = {}
    evaluations: dict[str, AggregateEvaluation] = {}
    for arm in ARMS:
        reports[arm], evaluations[arm] = _score_arm(rows[arm], source.qrels)
    primary = _paired(
        evaluations["baseline"],
        evaluations["parent"],
        metric=PRIMARY,
        samples=samples,
        seed=protocol["seed"],
    )
    comparisons = {
        "primary": primary,
        "exploratory": {
            metric: _paired(
                evaluations["baseline"],
                evaluations["parent"],
                metric=metric,
                samples=samples,
                seed=protocol["seed"],
            )
            for metric in SECONDARY
        },
    }
    timing = _timing(rows, source.qrels, order, samples=samples, seed=protocol["seed"])
    if phase == "dev":
        selection = _select(protocol, {arm: reports[arm]["metrics"] for arm in ARMS}, primary)
        selection.update(
            {
                "source": "DEV selection",
                "frozen_selection": _freeze(input_dir, protocol, selection).name,
                "reselected": False,
            }
        )
    else:
        if frozen is None:
            raise ValueError("regression requires frozen-selection.json")
        _validate_frozen(frozen, protocol)
        selection = {
            "selected_arm": "parent",
            "source": "frozen DEV selection",
            "gates": dict(frozen["gates"]),
            "reselected": False,
        }
    return {
        "schema_version": 1,
        "phase": phase,
        "protocol": dict(protocol),
        "scope": {
            "split": "train" if phase == "dev" else "test",
            "queries": count,
            "warm_queries": timing["warm_query_count"],
            "retrieval_only": True,
            "generation_evaluated": False,
            "test_exposed": phase == "regression",
            "blind_holdout_claim": False,
            "repeated_dev_exploratory": phase == "dev",
        },
        "source": {
            "project": protocol.get("project_source"),
            "second_project": protocol.get("second_project_source"),
            "function": protocol.get("source_function"),
            "adaptation": protocol.get("adaptation"),
        },
        "metric_definition": (
            "Use the first 20 raw rerank chunks, then scoring.evaluate_questions "
            "deduplicates documents before assigning metric ranks."
        ),
        "query_alignment": {
            "expected": count,
            "per_arm": len(order),
            "same_ids_and_order": True,
            "metadata_consistent": True,
        },
        "arms": reports,
        "comparisons": comparisons,
        "timing": timing,
        "selection": selection,
        "limitations": [
            protocol.get("limits"),
            "Repeated DEV selection is exploratory and uses previously exposed labels.",
            "One serial session cannot establish hardware-independent latency.",
            (
                "Token counts are post-call BGE batch-8 audit data, excluding latency; "
                "they are not a production context-admission measurement."
            ),
        ],
    }


def build_report(
    input_dir: Path,
    *,
    phase: str,
    protocol_path: Path,
    frozen_path: Path | None = None,
    data_path: Path | None = None,
) -> dict[str, Any]:
    protocol = _json(protocol_path)
    split = "train" if phase == "dev" else "test"
    frozen = None
    if phase == "regression":
        frozen_path = frozen_path or input_dir / "frozen-selection.json"
        frozen = _json(frozen_path)
    _label(input_dir, phase, protocol_path, frozen_path)
    source = load_scifact(data_path or DATA, split)
    count = COUNTS[split]
    if len(source.qrels) != count:
        raise ValueError(
            f"official {split} split has {len(source.qrels)} queries; expected {count}"
        )
    captures = {
        arm: (
            _json(input_dir / f"{phase}-{arm}.meta.json"),
            _jsonl(input_dir / f"{phase}-{arm}.jsonl"),
        )
        for arm in ARMS
    }
    _validate_captures(
        captures,
        protocol=protocol,
        phase=phase,
        qrels=source.qrels,
        expected_count=count,
        documents=set(source.corpus),
    )
    if frozen is not None:
        _validate_frozen(frozen, protocol)
    return score_captures(
        input_dir,
        protocol=protocol,
        phase=phase,
        source=source,
        expected_count=count,
        frozen=frozen,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--phase", choices=("dev", "regression"), required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--data", type=Path, default=DATA)
    args = parser.parse_args()
    if args.frozen is not None:
        parser.error(
            "the historical runner writes input/frozen-selection.json; do not pass --frozen"
        )
    report = build_report(
        args.input,
        phase=args.phase,
        protocol_path=args.protocol,
        frozen_path=args.frozen,
        data_path=args.data,
    )
    dump(args.output, report)
    print(args.output)


if __name__ == "__main__":
    main()
