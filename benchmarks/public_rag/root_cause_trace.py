"""Offline P0 replay of recorded paragraph-QA stages; no scoring or runtime."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.paper_qa_report import (
    _build_source_index,
    _evidence_diagnostics,
    _prepare_gold,
    _visible_diagnostics,
)
from benchmarks.public_rag.paragraph_qa_live import ARMS

ROOT = Path("benchmarks/public_rag/results/paragraph-qa-20261023")
OUT = Path("benchmarks/public_rag/results/rag-root-cause-20261024/trace-replay-1")
UNKNOWN = "UNKNOWN"


class OutputExistsError(FileExistsError):
    pass


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(items: Iterable[dict], source_map: dict | None = None) -> list[dict]:
    seen, result = set(), []
    for item in items:
        chunk_id = item.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        snippet = item.get("snippet")
        if not isinstance(snippet, str):
            snippet = item.get("text")
        raw = (source_map or {}).get(chunk_id, {})
        if not isinstance(snippet, str):
            snippet = raw.get("chunk_text")
        row = {
            "chunk_id": chunk_id,
            "rank": len(result) + 1,
            "score": item.get("score", item.get("rerank_score", item.get("retrieval_score"))),
        }
        if isinstance(snippet, str):
            row["snippet"] = snippet
        locator = item.get("locator")
        if isinstance(locator, dict):
            start, end = locator.get("char_start"), locator.get("char_end")
            if type(start) is int and type(end) is int:
                row["range"] = {"start": start, "end": end}
        # Paragraph envelopes are not exact chunk ranges. Do not invent a locator.
        result.append(row)
    return result


def _stage(
    records: list[dict] | None,
    source: str,
    identity: str,
    *,
    source_ids=None,
    latency=None,
    text_basis: str,
    range_basis: str,
):
    rows = records or []
    ids = [row["chunk_id"] for row in rows] if records is not None else None
    ids = source_ids if source_ids is not None else ids
    return {
        "status": "OBSERVED" if records is not None else UNKNOWN,
        "source": source,
        "identity": identity,
        "source_ids": ids,
        "ranks": (
            [row["rank"] for row in rows] if records is not None and source_ids is None else None
        ),
        "scores": [row["score"] for row in rows] if records is not None else None,
        "latency_ms": latency,
        "records": rows if records is not None else None,
        "text_basis": text_basis,
        "range_basis": range_basis,
    }


def _source_id_disappearance(stages: dict) -> dict:
    names = ("candidate_union", "fusion", "selection", "rerank", "final_evidence", "model_snippets")
    prior = None
    for name in names:
        ids = stages[name]["source_ids"]
        if ids is None:
            continue
        if prior is not None and not set(ids).issuperset(prior):
            return {
                "first_disappearance_stage": name,
                "disappeared_source_ids": sorted(prior - set(ids)),
                "possible_recovery_stage": next(
                    (
                        latter
                        for latter in names[names.index(name) + 1 :]
                        if prior - set(ids) <= set(stages[latter]["source_ids"] or [])
                    ),
                    None,
                ),
                "not_evidence_loss": True,
            }
        prior = set(ids)
    return {
        "first_disappearance_stage": None,
        "disappeared_source_ids": [],
        "possible_recovery_stage": None,
        "not_evidence_loss": True,
    }


def _annotation_substring_proxy_loss(visibility: dict) -> dict:
    names = tuple(
        name
        for name in (
            "parsed",
            "chunk",
            "candidate_union",
            "fusion",
            "selection",
            "rerank",
            "final_evidence",
            "model_snippets",
        )
        if name in visibility
    )
    observed = []
    for name in names:
        item = visibility[name]
        if item["status"] == "OBSERVED" and item["annotation_substring_coverage"] is not None:
            observed.append(
                {
                    "stage": name,
                    "coverage": item["annotation_substring_coverage"],
                    "zero": item["annotation_substring_visible"] == 0,
                }
            )
    drop_index = next(
        (
            index
            for index in range(1, len(observed))
            if observed[index]["coverage"] < observed[index - 1]["coverage"]
        ),
        None,
    )
    drop = observed[drop_index] if drop_index is not None else None
    recovery = (
        next(
            (
                item["stage"]
                for item in observed[drop_index + 1 :]
                if item["coverage"] > drop["coverage"]
            ),
            None,
        )
        if drop
        else None
    )
    return {
        "first_coverage_drop_stage": drop["stage"] if drop else None,
        "first_zero_exact_visibility_stage": next(
            (item["stage"] for item in observed if item["zero"]), None
        ),
        "possible_recovery_stage": recovery,
        "semantic_evidence_loss": UNKNOWN,
        "evidence_loss_claim": UNKNOWN,
        "note": (
            "Old _evidence_diagnostics whole-string substring proxy only. "
            "Zero exact visibility does not prove semantic evidence absence, the first coverage "
            "drop is not a task/reading failure, and possible recovery is descriptive."
        ),
    }


def _arm(
    *,
    paper_id: str,
    result: dict,
    query_id: str,
    gold: dict,
    sources,
    consensus: str,
    official: dict,
    messages: list[dict] | None,
    prepared_version: str | None,
    source_map: dict,
    arm_name: str | None = None,
) -> dict:
    retrieval = result.get("retrievals", [{}])[0] if result.get("retrievals") else {}
    trace = retrieval.get("trace") or {}
    dense = trace.get("dense")
    sparse = trace.get("sparse")
    fused = trace.get("fusion")
    selection = trace.get("selection")
    rerank = trace.get("rerank")

    def observed(record, *keys):
        return isinstance(record, dict) and all(key in record for key in keys)

    dense_rows = _rows(dense["results"], source_map) if observed(dense, "results") else None
    sparse_rows = _rows(sparse["results"], source_map) if observed(sparse, "results") else None
    union = {}
    if dense_rows is not None or sparse_rows is not None:
        for row in (dense_rows or []) + (sparse_rows or []):
            union.setdefault(row["chunk_id"], row)
    attempted = result.get("run_status") != "NOT_ATTEMPTED"
    evidence = (
        _rows(retrieval["evidence"], source_map)
        if attempted and isinstance(retrieval.get("evidence"), list)
        else None
    )
    visible = (
        _rows(result["visible_records"], source_map)
        if attempted and isinstance(result.get("visible_records"), list)
        else None
    )
    paragraph_rows, paragraph_ranges = {}, {}
    for raw in source_map.values():
        if raw.get("paper_id") != paper_id or (arm_name is not None and raw.get("arm") != arm_name):
            continue
        for paragraph in raw.get("paragraphs", []):
            key = paragraph.get("key")
            if not isinstance(key, str) or not key:
                continue
            paragraph_rows[key] = paragraph.get("text", "")
            start, end = paragraph.get("start"), paragraph.get("end")
            if type(start) is int and type(end) is int:
                paragraph_ranges[key] = {"start": start, "end": end}
    chunk_rows = []
    raw_chunks = [
        (chunk_id, raw)
        for chunk_id, raw in source_map.items()
        if raw.get("paper_id") == paper_id and (arm_name is None or raw.get("arm") == arm_name)
    ]
    for rank, (chunk_id, raw) in enumerate(
        sorted(
            raw_chunks,
            key=lambda item: (
                min((p.get("start", 0) for p in item[1].get("paragraphs", [])), default=0),
                item[0],
            ),
        ),
        1,
    ):
        row = {"chunk_id": chunk_id, "rank": rank, "score": None, "snippet": raw["chunk_text"]}
        starts = [p.get("start") for p in raw.get("paragraphs", [])]
        ends = [p.get("end") for p in raw.get("paragraphs", [])]
        if starts and ends and all(type(v) is int for v in starts + ends):
            row["paragraph_envelope"] = {"start": min(starts), "end": max(ends)}
        chunk_rows.append(row)
    trace_stages = {
        "dense": dense,
        "sparse": sparse,
        "fusion": fused,
        "selection": selection,
        "rerank": rerank,
    }
    single = {}
    for name, record in trace_stages.items():
        single[name] = _rows(record["results"], source_map) if observed(record, "results") else None
    parsed_rows = [
        {
            "chunk_id": key,
            "rank": rank,
            "score": None,
            "snippet": text,
            **({"range": paragraph_ranges[key]} if key in paragraph_ranges else {}),
        }
        for rank, (key, text) in enumerate(
            sorted(
                paragraph_rows.items(),
                key=lambda item: (
                    paragraph_ranges.get(item[0], {}).get("start", 10**12),
                    item[0],
                ),
            ),
            1,
        )
    ]
    stages = {
        "source": _stage(
            [],
            "frozen-dataset.json cases[]",
            "source identity only",
            source_ids=[paper_id],
            text_basis="UNKNOWN at source identity",
            range_basis="UNKNOWN at source identity",
        ),
        "parsed": _stage(
            parsed_rows,
            "source-map.json[chunk_id].paragraphs",
            "_build_source_index",
            text_basis="source-map paragraph text",
            range_basis="source-map paragraph start/end when recorded",
        ),
        "chunk": _stage(
            chunk_rows,
            "source-map.json chunk_text and paragraph spans",
            "_build_source_index",
            text_basis="source-map chunk_text",
            range_basis="paragraph-span envelope; not necessarily the exact chunk boundary",
        ),
        **{
            name: _stage(
                single[name],
                f"run.json trace.{name}.results",
                identity,
                latency=trace_stages[name].get("latency_ms")
                if observed(trace_stages[name])
                else None,
                text_basis="source-map chunk_text reconstruction",
                range_basis="UNKNOWN: trace has no range",
            )
            for name, identity in (
                ("dense", "packages.knowledge.retrieval.search (DENSE)"),
                ("sparse", "packages.knowledge.retrieval.search (SPARSE)"),
                ("fusion", "packages.knowledge.retrieval.fuse_reciprocal_rank"),
                ("selection", "packages.knowledge.retrieval.select_rerank_candidates"),
                ("rerank", "packages.knowledge.retrieval.search (HYBRID_RERANK)"),
            )
        },
        "candidate_union": _stage(
            (
                sorted(
                    union.values(),
                    key=lambda row: (
                        next(
                            (
                                index
                                for index, candidate in enumerate(dense_rows or [])
                                if candidate["chunk_id"] == row["chunk_id"]
                            ),
                            10**12,
                        ),
                        next(
                            (
                                index
                                for index, candidate in enumerate(sparse_rows or [])
                                if candidate["chunk_id"] == row["chunk_id"]
                            ),
                            10**12,
                        ),
                        row["chunk_id"],
                    ),
                )
                if union
                else []
            )
            if dense_rows is not None or sparse_rows is not None
            else None,
            "derived from recorded dense/sparse IDs",
            "offline stable ID union; not the nonlinear fusion",
            text_basis="source-map chunk_text reconstruction",
            range_basis="UNKNOWN: trace has no range",
        ),
        "final_evidence": _stage(
            evidence,
            "run.json retrievals[0].evidence",
            "RetrievalResult.evidence",
            text_basis="recorded evidence text",
            range_basis="recorded locator when present",
        ),
        "model_snippets": _stage(
            visible,
            "run.json visible_records",
            "paper_qa_visibility.extract_visible_records",
            text_basis="recorded model-visible snippet",
            range_basis="recorded locator when present",
        ),
        "tool_output_admission": _stage(
            None,
            "not separately logged",
            "UNKNOWN boundary",
            text_basis=UNKNOWN,
            range_basis=UNKNOWN,
        ),
        "runtime_admission": _stage(
            None,
            "not separately logged",
            "UNKNOWN boundary",
            text_basis=UNKNOWN,
            range_basis=UNKNOWN,
        ),
    }
    visibility = {}
    empty = _visible_diagnostics([], paper_id=paper_id, sources=sources)
    for name, stage in stages.items():
        records = stage["records"]
        diagnostic = (
            _evidence_diagnostics(gold, empty, sources=sources)
            if name in {"source", "parsed"} or records is None
            else _evidence_diagnostics(
                gold,
                _visible_diagnostics(records, paper_id=paper_id, sources=sources),
                sources=sources,
            )
        )
        evaluable = (
            diagnostic["evaluable"] and name not in {"source", "parsed"} and records is not None
        )
        visibility[name] = {
            "status": "OBSERVED" if evaluable else UNKNOWN,
            "annotation_substring_visible": diagnostic["exact_evidence_visible"]
            if evaluable
            else None,
            "annotation_substring_coverage": diagnostic["exact_evidence_item_coverage"]
            if evaluable
            else None,
            "strict_original_text_visible_rate": diagnostic["exact_evidence_item_coverage"]
            if evaluable
            else None,
            "annotation_paragraph_overlap": diagnostic["annotation_paragraph_overlap"]
            if evaluable
            else None,
            "note": "Normalized original-evidence substring only; not semantic sufficiency.",
        }
    return {
        "query_id": query_id,
        "paper_id": paper_id,
        "answerable_group": consensus,
        "run_status": result.get("run_status", UNKNOWN),
        "failure_code": result.get("failure_code"),
        "workflow_valid": result.get("workflow_valid"),
        "agent_version_id": result.get("agent_version_id"),
        "prepared_version_id": prepared_version,
        "version_match": result.get("agent_version_id") == prepared_version,
        "answer": result.get("answer"),
        "refused": official.get("refused"),
        "official_em": official.get("exact_match"),
        "official_f1": official.get("token_f1"),
        "usage": result.get("run_usage", {}),
        "physical_outcomes": result.get("physical_outcomes", []),
        "actual_model_messages": messages,
        "stages": stages,
        "visibility_by_stage": visibility,
        "annotation_substring_proxy_loss": _annotation_substring_proxy_loss(visibility),
        "recorded_source_id_disappearance": _source_id_disappearance(stages),
        "semantic_correctness": UNKNOWN,
        "relationship_correctness": UNKNOWN,
        "task_solved": UNKNOWN,
    }


def build_trace(*, rows, data, report, source_map, api_events, prepared) -> dict:
    sources = _build_source_index(source_map)
    cases = {case["query_id"]: case for case in data["cases"]}
    official = {case["query_id"]: case for case in report["cases"]}
    events = defaultdict(list)
    for event in api_events:
        if event.get("status") == "DISPATCHED" and isinstance(event.get("request"), dict):
            events[(event.get("query_id"), event.get("arm"))].append(event)
    details = []
    rows_by_id = {row["query_id"]: row for row in rows}
    if len(rows_by_id) != len(rows) or set(rows_by_id) - set(cases):
        raise ValueError("duplicate or unregistered run identity")
    for query_id in cases:
        row = rows_by_id.get(query_id, {"arms": {}})
        case, scored = cases[query_id], official[query_id]
        arms = {}
        for arm in ARMS:
            dispatched = sorted(events[(query_id, arm)], key=lambda e: e.get("model_step", -1))
            messages = dispatched[-1]["request"].get("messages") if dispatched else None
            version = prepared["versions"][case["paper_id"]][arm].get("version_id")
            arms[arm] = _arm(
                paper_id=case["paper_id"],
                result=row["arms"].get(arm, {"run_status": "NOT_ATTEMPTED"}),
                query_id=query_id,
                gold=_prepare_gold(case, sources),
                sources=sources,
                consensus=case["consensus"],
                official=scored["arms"][arm],
                messages=messages,
                prepared_version=version,
                source_map=source_map,
                arm_name=arm,
            )
        details.append({**case, "arms": arms})
    summaries = {}
    for arm in ARMS:
        statuses, losses, stages = Counter(), Counter(), {}
        for case in details:
            detail = case["arms"][arm]
            statuses[detail["run_status"]] += 1
            losses[
                detail["annotation_substring_proxy_loss"]["first_coverage_drop_stage"]
                or "NONE_OBSERVED"
            ] += 1
            for name, stage in detail["stages"].items():
                value = stages.setdefault(name, {"observed": 0, "unknown": 0, "latency": []})
                value["observed" if stage["status"] == "OBSERVED" else "unknown"] += 1
                if stage["latency_ms"] is not None:
                    value["latency"].append(stage["latency_ms"])
        for value in stages.values():
            latencies = value.pop("latency")
            value["mean_latency_ms"] = fmean(latencies) if latencies else None
        primary = report["arms"][arm]["primary"]
        summaries[arm] = {
            "planned_rows": len(data["cases"]),
            "run_statuses": dict(statuses),
            "annotation_substring_proxy_loss": dict(losses),
            "official": {
                key: primary.get(key)
                for key in (
                    "planned_queries",
                    "token_f1",
                    "exact_match",
                    "valid_response_fraction",
                    "failure_count",
                    "not_attempted_count",
                    "invalid_output_count",
                )
            },
            "stages": stages,
        }
    groups = {arm: dict(Counter(case["consensus"] for case in details)) for arm in ARMS}
    return {
        "schema": "rag_root_cause_trace_v1",
        "scope": (
            "Descriptive replay. Old _evidence_diagnostics joins snippets by rank and performs "
            "whole-string substring matching; exact_visibility=0 is an annotation-substring proxy "
            "loss and does not prove that evidence is truly absent. Structural answers can use the "
            "complete original baseline list even when this proxy is zero. Semantic sufficiency, "
            "semantic correctness, relationship correctness, task solved, and tool/runtime "
            "admission remain UNKNOWN. Source-range interval-union coverage is separate."
        ),
        "associations": {
            "official_metrics": "run report.json cases[].arms[]",
            "model_request": (
                "api-events.jsonl DISPATCHED events matched by query_id, arm, "
                "then latest model_step"
            ),
            "missing_request": UNKNOWN,
            "unattempted_row": "retained in the fixed denominator without failure attribution",
        },
        "stage_semantics": {
            "candidate_union": (
                "Stable ID union of recorded dense/sparse results for replay visibility only. "
                "It does not reproduce nonlinear RRF; fusion is the recorded runtime stage."
            ),
            "annotation_substring_proxy": (
                "Strict original-text substring over snippets concatenated in rank order. "
                "It is neither semantic sufficiency nor evidence-loss proof."
            ),
            "tool_output_admission": UNKNOWN,
            "runtime_admission": UNKNOWN,
        },
        "fixed_denominators": {"planned_rows": len(data["cases"]), "groups": groups},
        "summaries": summaries,
        "cases": details,
    }


def replay(source_root: Path = ROOT, run_name: str = "live-run-2", output_root: Path = OUT) -> dict:
    if output_root.exists():
        raise OutputExistsError(f"refusing to overwrite existing output: {output_root}")
    run = source_root / run_name
    event_lines = (run / "api-events.jsonl").read_text(encoding="utf-8").splitlines()
    return build_trace(
        rows=_load(run / "run.json"),
        data=_load(source_root / "frozen-dataset.json"),
        report=_load(run / "report.json"),
        source_map=_load(source_root / "source-map.json"),
        prepared=_load(source_root / "prepared.json"),
        api_events=[json.loads(line) for line in event_lines if line.strip()],
    )


def write(report: dict, output_root: Path) -> Path:
    if output_root.exists():
        raise OutputExistsError(f"refusing to overwrite existing output: {output_root}")
    output_root.mkdir(parents=True, exist_ok=False)
    path = output_root / "trace.json"
    # Keep partial output on failure as evidence; never remove a failed run.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--run-name", default="live-run-2")
    parser.add_argument("--output-root", type=Path, default=OUT)
    args = parser.parse_args()
    print(write(replay(args.source_root, args.run_name, args.output_root), args.output_root))


if __name__ == "__main__":
    main()
