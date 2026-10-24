"""Score recorded Onyx-style relevance selection; this program makes no API calls."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from random import Random
from statistics import fmean
from typing import Any

from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.scifact_report import quantile
from benchmarks.public_rag.scoring import (
    AggregateEvaluation,
    evaluate_questions,
    paired_bootstrap_comparison,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "data/external/rag-public-20261021/scifact/extracted/scifact"
EXPECTED_QUERIES = 809
STATUSES = ("VALID", "INVALID_OUTPUT", "BUDGET_FALLBACK", "UNKNOWN_OUTCOME", "HTTP_ERROR")
ATTEMPTED = frozenset(STATUSES) - {"BUDGET_FALLBACK"}
FAILURES = frozenset({"HTTP_ERROR", "UNKNOWN_OUTCOME"})
RULE = {
    "min_ndcg10_gain": 0.01,
    "max_recall5_loss": 0.005,
    "max_recall20_loss": 0.005,
    "require_primary_ci_above_zero": True,
    "min_valid_api_fraction": 0.95,
    "max_unknown_outcomes": 0,
}
type Hit = tuple[str, str, float]


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
        raise ValueError(f"cannot read run: {path}") from exc
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"{path.name} line {number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {number} must be an object")
        rows.append(row)
    return rows


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


def _protocol(protocol: Mapping[str, Any]) -> None:
    if (
        isinstance(protocol.get("seed"), bool)
        or not isinstance(protocol.get("seed"), int)
        or not isinstance(protocol.get("snapshot_id"), str)
        or not protocol["snapshot_id"]
        or not isinstance(protocol.get("model"), str)
        or not protocol["model"]
        or protocol.get("max_selected_sections") != 10
        or protocol.get("max_chunks_per_section") != 3
        or protocol.get("selection_rule") != RULE
    ):
        raise ValueError("protocol identity, caps, or selection rule differs")


def _meta(
    meta: Mapping[str, Any], protocol: Mapping[str, Any], count: int, split: str = "train"
) -> None:
    expected = {
        "protocol": dict(protocol),
        "queries": count,
        "split": split,
        "snapshot_id": protocol["snapshot_id"],
        "model": protocol["model"],
        "pairing": "cached baseline with actual incremental model calls",
        "generation_task": "retrieval selection only",
    }
    if any(meta.get(key) != value for key, value in expected.items()):
        raise ValueError("run metadata differs from the exact protocol")


def _budget(value: object, query: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{query} requires serialized ContextBudgetUsage")
    keys = (
        "context_limit",
        "reserved_output",
        "available_input",
        "estimated_input_before",
        "estimated_input_after",
        "dropped_exchange_count",
    )
    counts = {key: _integer(value.get(key), f"{query} budget.{key}") for key in keys}
    truncated = value.get("truncated_by_category")
    if not isinstance(truncated, Mapping):
        raise ValueError(f"{query} budget.truncated_by_category must be an object")
    for category, count in truncated.items():
        if not isinstance(category, str) or not category:
            raise ValueError(f"{query} budget has an invalid category")
        _integer(count, f"{query} budget.truncated_by_category.{category}")
    if (
        counts["available_input"] != counts["context_limit"] - counts["reserved_output"]
        or counts["estimated_input_after"] > counts["available_input"]
        or not isinstance(value.get("evidence_semantically_preserved"), bool)
        or (not value["evidence_semantically_preserved"] and not truncated)
    ):
        raise ValueError(f"{query} has inconsistent budget accounting")
    if any(truncated.values()) and value["evidence_semantically_preserved"]:
        raise ValueError(f"{query} budget reports truncation with preserved evidence")


def _reasoning(usage: Mapping[str, Any]) -> int | None:
    details = usage.get("completion_tokens_details")
    if details is None:
        value = usage.get("reasoning_tokens")
        return None if value is None else _integer(value, "reasoning_tokens")
    if not isinstance(details, Mapping):
        raise ValueError("usage details must be an object")
    value = details.get("reasoning_tokens")
    if value is not None:
        return _integer(value, "reasoning_tokens")
    value = usage.get("reasoning_tokens")
    return None if value is None else _integer(value, "reasoning_tokens")


def _usage(value: object, query: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"{query} usage must be an object or null")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in value:
            _integer(value[key], f"{query} usage.{key}")
    _reasoning(value)


def _ranking(row: Mapping[str, Any], field: str) -> tuple[Hit, ...]:
    value = row.get(field)
    if not isinstance(value, list) or len(value) != 20:
        raise ValueError(f"{row.get('query_id')} {field} must contain 20 payloads")
    hits: list[Hit] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field} payload must be an object")
        chunk, document, score = raw.get("chunk_id"), raw.get("document_id"), raw.get("score")
        if not isinstance(chunk, str) or not chunk or not isinstance(document, str) or not document:
            raise ValueError(f"{field} payload requires non-empty IDs")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score):
            raise ValueError(f"{field} score must be finite")
        hits.append((chunk, document, float(score)))
    if len({hit[0] for hit in hits}) != 20:
        raise ValueError(f"{field} contains duplicate chunks")
    return tuple(hits)


def _row(
    row: Mapping[str, Any],
    *,
    query: str,
    ordinal: int,
    corpus: set[str],
    source_map: dict[str, str],
) -> tuple[tuple[Hit, ...], tuple[Hit, ...]]:
    fields = {
        "query_id",
        "ordinal",
        "baseline_ranking",
        "final_ranking",
        "section_source_ids",
        "section_chunk_ids",
        "selection",
        "status",
        "attempted",
        "request_ms",
        "usage",
        "budget",
        "http_status",
    }
    if not fields.issubset(row) or row["query_id"] != query or row.get("ordinal") != ordinal:
        raise ValueError(f"{query} required fields or ordinal differ from protocol")
    status, attempted = row.get("status"), row.get("attempted")
    if (
        status not in STATUSES
        or not isinstance(attempted, bool)
        or attempted != (status != "BUDGET_FALLBACK")
    ):
        raise ValueError(f"{query} has invalid status or attempted flag")
    _finite(row.get("request_ms"), f"{query} request_ms")
    _usage(row.get("usage"), query)
    _budget(row.get("budget"), query)
    if "response_model_verified" in row and not isinstance(row["response_model_verified"], bool):
        raise ValueError(f"{query} response_model_verified must be boolean")
    if status == "VALID" and row["budget"]["evidence_semantically_preserved"] is not True:
        raise ValueError(f"{query} VALID response changed admitted evidence")
    if row.get("http_status") is not None:
        _integer(row["http_status"], f"{query} http_status")
    baseline, final = _ranking(row, "baseline_ranking"), _ranking(row, "final_ranking")
    baseline_map, final_map = {hit[0]: hit for hit in baseline}, {hit[0]: hit for hit in final}
    if set(baseline_map) != set(final_map):
        raise ValueError(f"{query} final payload set differs from baseline")
    for hit in baseline:
        chunk, document, _ = hit
        if document not in corpus:
            raise ValueError(f"{query} payload maps to unknown corpus document")
        if source_map.setdefault(chunk, document) != document or final_map[chunk] != hit:
            raise ValueError(f"{query} final payload ID, source, or score drifted")
    sources, groups, selected = (
        row.get("section_source_ids"),
        row.get("section_chunk_ids"),
        row.get("selection"),
    )
    if (
        not isinstance(sources, list)
        or len(set(sources)) != len(sources)
        or sources != list(dict.fromkeys(hit[1] for hit in baseline))
        or not all(isinstance(source, str) and source for source in sources)
        or not isinstance(groups, list)
        or len(groups) != len(sources)
        or not isinstance(selected, list)
        or any(isinstance(index, bool) or not isinstance(index, int) for index in selected)
        or len(selected) > 10
        or len(set(selected)) != len(selected)
        or (status == "VALID" and any(not 0 <= index < len(sources) for index in selected))
    ):
        raise ValueError(f"{query} has invalid section selection")
    for source, chunks in zip(sources, groups, strict=True):
        if (
            not isinstance(chunks, list)
            or not chunks
            or len(chunks) > 3
            or len(set(chunks)) != len(chunks)
            or any(
                not isinstance(chunk, str)
                or chunk not in baseline_map
                or baseline_map[chunk][1] != source
                for chunk in chunks
            )
        ):
            raise ValueError(f"{query} section chunks are not a valid baseline source subset")
        if set(chunks) != {chunk for chunk, document, _ in baseline if document == source}:
            expected_visible = [chunk for chunk, document, _ in baseline if document == source][:3]
            if set(chunks) != set(expected_visible):
                raise ValueError(
                    f"{query} section does not contain the frozen top3 matching children"
                )
    if status != "VALID" or not selected:
        if final != baseline:
            raise ValueError(f"{query} fallback or empty selection must preserve baseline order")
    else:
        promoted = [
            chunk
            for index in selected
            for chunk, document, _ in baseline
            if document == sources[index]
        ]
        promoted_set = set(promoted)
        expected = promoted + [chunk for chunk, _, _ in baseline if chunk not in promoted_set]
        if [chunk for chunk, _, _ in final] != expected:
            raise ValueError(f"{query} VALID order differs from model parent order")
    return baseline, final


def _timing(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean_ms": fmean(values) if values else None,
        "p50_ms": quantile(list(values), 0.5),
        "p95_ms": quantile(list(values), 0.95),
    }


def _compare(baseline: AggregateEvaluation, candidate: AggregateEvaluation, metric: str, **kw: Any):
    result = paired_bootstrap_comparison(baseline, candidate, metric=metric, **kw)
    return {
        "metric": metric,
        "estimate": result.estimate,
        "ci95_low": result.ci_low,
        "ci95_high": result.ci_high,
        "sample_count": result.sample_count,
        "group_count": result.group_count,
        "confidence": result.confidence,
        "inference_valid": result.group_count >= 2,
        "direction": "selection minus cached baseline",
    }


def _observed_usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attempted = [row for row in rows if row["attempted"]]
    observed = [row["usage"] for row in attempted if row["usage"] is not None]
    tokens = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [usage[key] for usage in observed if key in usage]
        tokens[key] = {
            "sum": sum(values) if values else None,
            "observed_query_count": len(values),
            "unknown_query_count": len(attempted) - len(values),
        }
    reasoning = [value for usage in observed if (value := _reasoning(usage)) is not None]
    tokens["reasoning_tokens"] = {
        "sum": sum(reasoning) if reasoning else None,
        "provider_reported_query_count": len(reasoning),
        "not_reported_query_count": len(attempted) - len(reasoning),
    }
    verified = [row["response_model_verified"] for row in rows if "response_model_verified" in row]
    output = {
        "attempted_query_count": len(attempted),
        "observed_usage_query_count": len(observed),
        "unknown_usage_query_count": len(attempted) - len(observed),
        "malformed_http_200_usage_query_count": sum(
            row["status"] == "HTTP_ERROR" and row["http_status"] == 200 and row["usage"] is not None
            for row in rows
        ),
        "tokens": tokens,
        "cost": "not reported; no pricing snapshot is bound",
    }
    if verified:
        output["response_model_verification"] = {
            "verified_count": sum(verified),
            "unverified_count": len(verified) - sum(verified),
            "reported_count": len(verified),
            "not_reported_count": len(rows) - len(verified),
            "guarantee": (
                "provider identity is unverified for responses without this field"
                if len(verified) < len(rows)
                else "all captured responses report an identity check"
            ),
        }
    return output


def _select(
    protocol: Mapping[str, Any],
    baseline: AggregateEvaluation,
    candidate: AggregateEvaluation,
    primary: Mapping[str, Any],
    outcomes: Mapping[str, int],
    count: int,
) -> dict[str, Any]:
    rule = protocol["selection_rule"]
    gates = {
        "nDCG@10_gain": {
            "observed": candidate.metrics["nDCG@10"] - baseline.metrics["nDCG@10"],
            "threshold": rule["min_ndcg10_gain"],
        },
        "primary_ci_above_zero": {
            "observed_ci95_low": primary["ci95_low"],
            "inference_valid": primary["inference_valid"],
        },
        "Recall@5_loss": {
            "observed": baseline.metrics["Recall@5"] - candidate.metrics["Recall@5"],
            "threshold": rule["max_recall5_loss"],
        },
        "Recall@20_loss": {
            "observed": baseline.metrics["Recall@20"] - candidate.metrics["Recall@20"],
            "threshold": rule["max_recall20_loss"],
        },
        "valid_api_fraction": {
            "observed": outcomes["VALID"] / count,
            "threshold": rule["min_valid_api_fraction"],
        },
        "unknown_outcomes": {
            "observed": outcomes["UNKNOWN_OUTCOME"],
            "maximum": rule["max_unknown_outcomes"],
        },
    }
    values = (
        gates["nDCG@10_gain"]["observed"] >= gates["nDCG@10_gain"]["threshold"],
        gates["primary_ci_above_zero"]["inference_valid"]
        and gates["primary_ci_above_zero"]["observed_ci95_low"] > 0,
        gates["Recall@5_loss"]["observed"] <= gates["Recall@5_loss"]["threshold"],
        gates["Recall@20_loss"]["observed"] <= gates["Recall@20_loss"]["threshold"],
        gates["valid_api_fraction"]["observed"] >= gates["valid_api_fraction"]["threshold"],
        gates["unknown_outcomes"]["observed"] <= gates["unknown_outcomes"]["maximum"],
    )
    for gate, passed in zip(gates.values(), values, strict=True):
        gate["passed"] = passed
    return {
        "selected_arm": "onyx_selection" if all(values) else "baseline",
        "all_gates_passed": all(values),
        "gates": gates,
        "frozen_selection": "frozen-selection.json",
        "scope": "quality screen only; independent confirmation remains required",
        "production_default_changed": False,
        "model_allowlist_changed": False,
    }


def score_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    meta: Mapping[str, Any],
    protocol: Mapping[str, Any],
    qrels: Mapping[str, Mapping[str, int]],
    corpus: Mapping[str, object] | set[str],
    expected_count: int = EXPECTED_QUERIES,
    samples: int = 10_000,
    split: str = "train",
) -> dict[str, Any]:
    """Validate one complete capture and return its report."""

    if samples < 1:
        raise ValueError("bootstrap samples must be positive")
    _protocol(protocol)
    if split not in {"train", "test"}:
        raise ValueError("score_rows split must be train or test")
    _meta(meta, protocol, expected_count, split)
    order = sorted(qrels)
    Random(protocol["seed"]).shuffle(order)
    ids = [row.get("query_id") for row in rows]
    if len(qrels) != expected_count or len(rows) != expected_count or set(ids) != set(order):
        raise ValueError("needs attention: run and qrels must cover one complete split")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate query IDs in concurrent capture")
    by_query = {row["query_id"]: row for row in rows}
    rows = [by_query[query] for query in order]
    if sum(row.get("status") == "UNKNOWN_OUTCOME" for row in rows):
        raise ValueError("needs attention: UNKNOWN_OUTCOME prevents a full report")
    source_map: dict[str, str] = {}
    corpus_ids = set(corpus)
    captures = {
        query: _row(
            row,
            query=query,
            ordinal=ordinal,
            corpus=corpus_ids,
            source_map=source_map,
        )
        for ordinal, (query, row) in enumerate(zip(order, rows, strict=True))
    }
    baseline_eval = evaluate_questions(
        qrels,
        {query: tuple(hit[1] for hit in baseline) for query, (baseline, _) in captures.items()},
    )
    candidate_eval = evaluate_questions(
        qrels,
        {query: tuple(hit[1] for hit in final) for query, (_, final) in captures.items()},
    )
    if (
        baseline_eval.evaluated_count != expected_count
        or candidate_eval.evaluated_count != expected_count
    ):
        raise ValueError("every qrels-backed query must contribute to the metrics")
    primary = _compare(
        baseline_eval,
        candidate_eval,
        "nDCG@10",
        sample_count=samples,
        confidence=0.95,
        seed=protocol["seed"],
    )
    outcomes = Counter(row["status"] for row in rows)
    outcomes = {status: outcomes[status] for status in STATUSES}
    attempted = [float(row["request_ms"]) for row in rows if row["attempted"]]
    missing_model_identity = any("response_model_verified" not in row for row in rows)
    return {
        "schema_version": 1,
        "protocol": dict(protocol),
        "scope": {
            "split": split,
            "queries": expected_count,
            "all_queries_included": True,
            "fallback_rows_retained": True,
            "repeated_dev_exploratory": split == "train",
            "generation_evaluated": False,
        },
        "metrics": {
            "definition": "first 20 raw children, then evaluate_questions document deduplication",
            "baseline": dict(baseline_eval.metrics),
            "candidate": dict(candidate_eval.metrics),
            "evaluated_count": candidate_eval.evaluated_count,
            "comparisons": {
                "primary": primary,
                "exploratory": {
                    metric: _compare(
                        baseline_eval,
                        candidate_eval,
                        metric,
                        sample_count=samples,
                        confidence=0.95,
                        seed=protocol["seed"],
                    )
                    for metric in ("Recall@5", "Recall@20", "MRR@10")
                },
            },
        },
        "api_outcomes": {
            "actual_api_calls": len(attempted),
            "status_counts": outcomes,
            "fallback_counts": {
                key: outcomes[key] for key in ("INVALID_OUTPUT", "BUDGET_FALLBACK", "HTTP_ERROR")
            },
            "provider_fallback_count": outcomes["INVALID_OUTPUT"] + outcomes["HTTP_ERROR"],
            "valid_empty_selection_fallback_count": sum(
                row["status"] == "VALID" and not row["selection"] for row in rows
            ),
        },
        "timing_ms": {
            "attempted": _timing(attempted),
            "success": _timing(
                [float(row["request_ms"]) for row in rows if row["status"] in ATTEMPTED - FAILURES]
            ),
            "failure": _timing(
                [float(row["request_ms"]) for row in rows if row["status"] in FAILURES]
            ),
            "historical_baseline_added": False,
            "end_to_end_speed_claim": False,
        },
        "provider_usage": _observed_usage(rows),
        "context_budget": {
            "fallback_count": outcomes["BUDGET_FALLBACK"],
            "truncated_count": sum(
                any(row["budget"]["truncated_by_category"].values()) for row in rows
            ),
            "dropped_exchange_count": sum(row["budget"]["dropped_exchange_count"] for row in rows),
            "evidence_semantically_preserved_count": sum(
                row["budget"]["evidence_semantically_preserved"] for row in rows
            ),
            "estimated_input_before_total": sum(
                row["budget"]["estimated_input_before"] for row in rows
            ),
            "estimated_input_after_total": sum(
                row["budget"]["estimated_input_after"] for row in rows
            ),
        },
        "selection": _select(
            protocol, baseline_eval, candidate_eval, primary, outcomes, expected_count
        ),
        "source": {
            "project": protocol.get("project_source"),
            "mechanism": protocol.get("mechanism"),
            "adaptation": protocol.get("adaptations"),
        },
        "limitations": [
            protocol.get("limits"),
            "This is not a production default, model allowlist, or independent-confirmation claim.",
            "The bootstrap resamples gold-overlap query groups from this repeated DEV split.",
            (
                "Provider identity is unverified for responses without response_model_verified."
                if missing_model_identity
                else "Every captured response includes a provider identity check."
            ),
        ],
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _freeze(input_dir: Path, protocol: Mapping[str, Any], selection: Mapping[str, Any]) -> Path:
    payload = {
        "protocol": dict(protocol),
        "selected_arm": selection["selected_arm"],
        "gates": dict(selection["gates"]),
    }
    path = input_dir / "frozen-selection.json"
    if path.exists() and _json(path) != payload:
        raise ValueError("existing frozen-selection.json differs")
    if not path.exists():
        _write_json(path, payload)
    return path


def build_report(
    input_dir: Path, *, protocol_path: Path, data_path: Path = DEFAULT_DATA, samples: int = 10_000
) -> dict[str, Any]:
    """Read captures, log label access before loading labels, and freeze the screen."""

    protocol = _json(protocol_path)
    _protocol(protocol)
    meta = _json(input_dir / "run.meta.json")
    _meta(meta, protocol, EXPECTED_QUERIES)
    rows = _jsonl(input_dir / "run.jsonl")
    event = {
        "event": "ONYX_DEV_SCORING_LABEL_ACCESS",
        "at": datetime.now(UTC).isoformat(),
        "split": "train",
        "protocol": str(protocol_path),
    }
    with (input_dir / "label-access.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    source = load_scifact(data_path, "train")
    if len(source.qrels) != EXPECTED_QUERIES or set(source.queries) != set(source.qrels):
        raise ValueError("official SciFact train split must contain 809 matching queries and qrels")
    report = score_rows(
        rows,
        meta=meta,
        protocol=protocol,
        qrels=source.qrels,
        corpus=source.corpus,
        samples=samples,
    )
    _freeze(input_dir, protocol, report["selection"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    report = build_report(args.input, protocol_path=args.protocol, data_path=args.data)
    _write_json(args.output, report)
    print(args.output)


if __name__ == "__main__":
    main()
