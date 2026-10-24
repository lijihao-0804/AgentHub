"""Reusable metrics for scenario-style public RAG evaluation."""

from __future__ import annotations

from math import isfinite
from random import Random
from statistics import fmean


def evidence_metrics(evidence_sets: list[list[str]], retrieved_ids: list[str]) -> dict:
    """Score retrieval against alternate annotator evidence sets.

    Empty or all-empty evidence annotations are not evaluable.  An empty
    retrieval is evaluable when gold evidence exists, but it cannot receive
    recall, completeness, or precision credit.
    """

    retrieved = set(retrieved_ids)
    nonempty_gold = [set(ids) for ids in evidence_sets if ids]
    if not nonempty_gold:
        return {
            "best_evidence_recall": 0.0,
            "complete_evidence_set": 0,
            "evidence_precision": 0.0,
            "evaluable": False,
        }

    acceptable = set().union(*nonempty_gold)
    return {
        "best_evidence_recall": max(
            len(retrieved & evidence) / len(evidence) for evidence in nonempty_gold
        ),
        "complete_evidence_set": int(any(evidence <= retrieved for evidence in nonempty_gold)),
        "evidence_precision": len(retrieved & acceptable) / len(retrieved) if retrieved else 0.0,
        "evaluable": True,
    }


def paired_group_ci(
    rows: list[dict],
    metric: str,
    baseline_arm: str,
    candidate_arm: str,
    group_key: str = "group_id",
    seed: int = 20261023,
    samples: int = 5000,
    confidence: float = 0.95,
) -> dict:
    """Return a query-weighted, group-clustered paired percentile bootstrap.

    The reported difference is ``candidate - baseline``.  Whole groups are
    resampled with replacement; repeated groups retain their queries' weight.
    One cluster is descriptive only and is marked as invalid for inference.
    """

    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
        raise ValueError("samples must be a positive integer")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not isfinite(confidence)
        or not 0 < confidence < 1
    ):
        raise ValueError("confidence must be between 0 and 1")
    if baseline_arm == candidate_arm:
        raise ValueError("baseline_arm and candidate_arm must differ")

    seen: set[object] = set()
    groups: dict[object, list[tuple[float, float]]] = {}
    for row in rows:
        query_id = row["query_id"]
        try:
            duplicate = query_id in seen
        except TypeError as exc:
            raise ValueError("query_id must be hashable") from exc
        if duplicate:
            raise ValueError(f"duplicate query_id: {query_id!r}")
        seen.add(query_id)

        group_id = row[group_key]
        arms = row["arms"]
        if baseline_arm not in arms or candidate_arm not in arms:
            raise ValueError(f"unpaired query: {query_id!r}")
        try:
            baseline_value = arms[baseline_arm][metric]
            candidate_value = arms[candidate_arm][metric]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"missing metric {metric!r} for query: {query_id!r}") from exc
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            for value in (baseline_value, candidate_value)
        ):
            raise ValueError(f"metric values must be finite for query: {query_id!r}")

        try:
            groups.setdefault(group_id, []).append((baseline_value, candidate_value))
        except TypeError as exc:
            raise ValueError("group_id must be hashable") from exc

    if not groups:
        raise ValueError("paired_group_ci requires at least one paired query")

    pairs = [pair for group in groups.values() for pair in group]
    baseline_values = [baseline for baseline, _ in pairs]
    candidate_values = [candidate for _, candidate in pairs]
    baseline_mean = fmean(baseline_values)
    candidate_mean = fmean(candidate_values)
    difference = fmean(
        candidate - baseline
        for baseline, candidate in zip(baseline_values, candidate_values, strict=True)
    )

    cluster_stats = [
        (sum(candidate - baseline for baseline, candidate in group), len(group))
        for group in groups.values()
    ]
    rng = Random(seed)
    bootstrap_differences = []
    for _ in range(samples):
        numerator = 0.0
        denominator = 0
        for _ in cluster_stats:
            total, count = cluster_stats[rng.randrange(len(cluster_stats))]
            numerator += total
            denominator += count
        bootstrap_differences.append(numerator / denominator)

    tail = (1.0 - confidence) / 2.0
    low = _percentile(bootstrap_differences, tail)
    high = _percentile(bootstrap_differences, 1.0 - tail)
    return {
        "baseline_mean": baseline_mean,
        "candidate_mean": candidate_mean,
        "difference": difference,
        "delta": difference,
        "ci_low": low,
        "ci_high": high,
        "confidence": confidence,
        "ci95_low": low,
        "ci95_high": high,
        "queries": len(rows),
        "groups": len(groups),
        "inference_valid": len(groups) >= 2,
    }


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
