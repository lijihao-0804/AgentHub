"""One development correction: preserve admitted anchors before source expansion.

Reuses the frozen real GPU retrievals verbatim. No re-embedding, model calls,
new index, new parameter values or source/answer-based anchor selection.
"""

from __future__ import annotations

import argparse
import json

from benchmarks.public_rag.experimental.paragraph_recovery import preserve_then_expand
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.paragraph_recovery_run import (
    ADVANCE_GATE,
    CONFIG,
    DATA,
    SEED,
    coverage,
    read,
    write,
)
from benchmarks.public_rag.paragraph_recovery_run import (
    ROOT as SOURCE,
)
from benchmarks.public_rag.reader_isolation import clean_build

ROOT = SOURCE.parent / "paragraph-preserving-1"


def freeze():
    commit = clean_build()
    previous = read(SOURCE / "pilot-1/report.json")
    if previous["decision"] != "STOP_NO_ANSWER_EXPERIMENT":
        raise ValueError("this correction belongs after the recorded rejected parent policy")
    ROOT.mkdir()
    write(
        ROOT / "protocol.json",
        {
            "state": "FROZEN_DEVELOPMENT_ANCHOR_PRESERVATION",
            "build_commit": commit,
            "source_protocol": (SOURCE / "protocol.json").as_posix(),
            "source_retrievals": (SOURCE / "pilot-1/retrievals.jsonl").as_posix(),
            "registered_queries": previous["registered"],
            "config": CONFIG,
            "advance_gate": ADVANCE_GATE,
            "hypothesis": (
                "Parent replacement discarded already admitted facts. Reserve the baseline "
                "first; accept only budget-feasible source supersets, without changing caps."
            ),
            "not_independent_confirmation": True,
            "ranking_and_source_frozen": True,
            "new_gpu_inferences": 0,
            "new_answer_model_calls": 0,
            "stop": (
                "If the same advance gate fails, close reading recovery; no more variants."
            ),
        },
    )
    print(json.dumps({"registered_queries": previous["registered"], "new_inferences": 0}))


def execute():
    protocol = read(ROOT / "protocol.json")
    if (
        clean_build() != protocol["build_commit"]
        or protocol["config"] != CONFIG
        or protocol["advance_gate"] != ADVANCE_GATE
    ):
        raise ValueError("registered implementation or budget changed")
    data, prepared = read(DATA), read(SOURCE / "prepared.json")
    papers = {p["paper_id"]: p for p in prepared["papers"]}
    chunks = {c["chunk_id"]: c for p in papers.values() for c in p["chunks"]}
    rows = [
        json.loads(line)
        for line in (SOURCE / "pilot-1/retrievals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if [r["query_id"] for r in rows] != [c["query_id"] for c in data["cases"]]:
        raise ValueError("frozen retrieval population changed")
    out = ROOT / "pilot-1"
    out.mkdir()
    evaluated, invariants = [], []
    with (out / "views.jsonl").open("x", encoding="utf-8") as stream:
        for case, row in zip(data["cases"], rows, strict=True):
            paper = papers[case["paper_id"]]
            anchors = [chunks[r["chunk_id"]] for r in row["rerank"][: CONFIG["anchors"]]]
            baseline = row["arms"]["leaf"]
            candidate = preserve_then_expand(
                baseline["records"],
                anchors,
                paper["article"],
                paper["spans"],
                paper["paper_id"],
                paper["revision_id"],
                max_chars=CONFIG["parent_max_chars"],
                max_bytes=CONFIG["record_array_bytes"],
            )
            item = {
                "query_id": case["query_id"],
                "paper_id": case["paper_id"],
                "arms": {"leaf": baseline, "preserving": candidate},
            }
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            invariants.append(candidate)
            evaluated.append(
                {
                    "query_id": case["query_id"],
                    "paper_id": case["paper_id"],
                    "arms": {a: coverage(case, paper, v) for a, v in item["arms"].items()},
                }
            )
    eligible = [r for r in evaluated if all(v["evaluable"] for v in r["arms"].values())]
    stats, advance = None, False
    if eligible:
        raw = grouped_difference(
            {r["query_id"]: float(r["arms"]["leaf"]["complete"]) for r in eligible},
            {r["query_id"]: float(r["arms"]["preserving"]["complete"]) for r in eligible},
            {r["query_id"]: r["paper_id"] for r in eligible},
            seed=SEED,
        )
        stats = {
            "delta": raw["estimate_ms"],
            "ci95": [raw["ci95_low_ms"], raw["ci95_high_ms"]],
            "queries": raw["queries"],
            "paper_groups": raw["groups"],
            "units": "fraction",
        }
        advance = (
            stats["delta"] >= ADVANCE_GATE["min_complete_annotation_delta"]
            and stats["ci95"][0] > ADVANCE_GATE["ci95_lower_above"]
        )
    report = {
        "state": "COMPLETE",
        "registered": len(rows),
        "completed": len(evaluated),
        "evaluable": len(eligible),
        "unknown_or_no_positive_annotation": len(rows) - len(eligible),
        "complete_counts": {
            a: sum(r["arms"][a]["complete"] for r in eligible) for a in ["leaf", "preserving"]
        },
        "paired_coordinate_difference": stats,
        "advance_to_answers": advance,
        "decision": "QUALIFIES_ANSWER_EXPERIMENT" if advance else "STOP_READING_RECOVERY_DIRECTION",
        "baseline_coordinates_preserved_rows": sum(
            v["baseline_coordinates_preserved"] for v in invariants
        ),
        "expanded_records": sum(v["expanded_records"] for v in invariants),
        "budget_skipped_expansions": sum(v["expansions_skipped_for_budget"] for v in invariants),
        "max_record_array_bytes": max(v["wire_bytes"] for v in invariants),
        "new_gpu_inferences": 0,
        "new_answer_model_calls": 0,
        "production_promoted": False,
        "semantic_sufficiency": "UNKNOWN",
        "cases": evaluated,
    }
    write(out / "report.json", report)
    write(
        out / "completed.json",
        {
            k: report[k]
            for k in ["state", "registered", "completed", "decision", "new_answer_model_calls"]
        },
    )
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "execute"])
    args = parser.parse_args()
    freeze() if args.action == "freeze" else execute()
