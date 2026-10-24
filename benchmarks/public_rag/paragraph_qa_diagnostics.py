"""Posthoc descriptive stage diagnosis; human evidence never enters generation."""

from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import fmean

from benchmarks.public_rag.paper_qa_report import (
    _build_source_index,
    _evidence_diagnostics,
    _prepare_gold,
    _visible_diagnostics,
)
from benchmarks.public_rag.paragraph_qa_audit import read
from benchmarks.public_rag.paragraph_qa_live import ARMS, ROOT
from benchmarks.public_rag.scenario_runner import dump


def diagnose(run_name):
    data, sources = read(ROOT / "frozen-dataset.json"), read(ROOT / "source-map.json")
    folder = ROOT / run_name
    rows, report = read(folder / "run.json"), read(folder / "report.json")
    source_index = _build_source_index(sources)
    cases = {c["query_id"]: c for c in data["cases"]}
    aggregated, details = defaultdict(list), []
    compliance = {arm: {"recorded_searches": 0, "original_question_searches": 0} for arm in ARMS}
    for row in rows:
        case = cases[row["query_id"]]
        gold = _prepare_gold(case, source_index)
        for arm, result in row["arms"].items():
            retrieval = result["retrievals"][0] if result["retrievals"] else None
            if retrieval:
                compliance[arm]["recorded_searches"] += 1
                compliance[arm]["original_question_searches"] += (
                    retrieval["query"]["text"] == case["question"]
                )
            if case["consensus"] != "answerable":
                continue
            stages = {}
            for name in ("dense", "sparse", "fusion", "rerank"):
                hits = retrieval["trace"][name]["results"] if retrieval else []
                stages[name + "_full_chunks"] = [
                    {"chunk_id": hit["chunk_id"], "snippet": sources[hit["chunk_id"]]["chunk_text"]}
                    for hit in hits
                ]
            stages["retriever_final_full_chunks"] = (
                [
                    {"chunk_id": hit["chunk_id"], "snippet": hit["text"]}
                    for hit in retrieval["evidence"]
                ]
                if retrieval
                else []
            )
            stages["actual_model_snippets"] = result["visible_records"]
            for stage, records in stages.items():
                visible = _visible_diagnostics(
                    records, paper_id=case["paper_id"], sources=source_index
                )
                assert not visible["errors"]
                value = _evidence_diagnostics(gold, visible, sources=source_index)
                if not value["evaluable"]:
                    continue
                entry = {
                    "query_id": case["query_id"],
                    "arm": arm,
                    "stage": stage,
                    "records": len(records),
                    "snippet_chars": sum(len(r["snippet"]) for r in records),
                    **value,
                }
                aggregated[(arm, stage)].append(entry)
                details.append(entry)
    stages = {
        arm: {
            stage: {
                "questions": len(values),
                **{
                    metric: fmean(v[metric] for v in values)
                    for metric in (
                        "annotation_paragraph_overlap",
                        "exact_evidence_visible",
                        "exact_evidence_item_coverage",
                        "records",
                        "snippet_chars",
                    )
                },
            }
            for (a, stage), values in aggregated.items()
            if a == arm
        }
        for arm in ARMS
    }
    changes = sorted(
        [
            {
                "query_id": c["query_id"],
                "question": c["question"],
                "paper_id": c["paper_id"],
                "fixed_f1": c["arms"]["fixed"]["token_f1"],
                "structural_f1": c["arms"]["structural"]["token_f1"],
                "delta": c["arms"]["structural"]["token_f1"] - c["arms"]["fixed"]["token_f1"],
            }
            for c in report["cases"]
        ],
        key=lambda c: c["delta"],
    )
    dump(
        folder / "stage-coverage.json",
        {
            "scope": (
                "Posthoc descriptive diagnosis, not isolated mechanism causality. "
                "Full candidate chunks are internal stage views; only actual_model_snippets "
                "are visible to the answer model. Paragraph overlap is coarse; strict "
                "substring visibility is not semantic support."
            ),
            "stages": stages,
            "query_compliance": compliance,
            "answer_changes": changes,
            "cases": details,
        },
    )
    print(stages)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", default="live-run-1")
    diagnose(parser.parse_args().run_name)
