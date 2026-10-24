"""Describe source coverage under existing tool/API raw-chunk limits.

These are consumer compatibility diagnostics, not extra model variants or gates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.budget_ablation import exposure
from benchmarks.public_rag.data import load_beir
from benchmarks.public_rag.scifact_runner import dump
from benchmarks.public_rag.scoring import evaluate_questions

# Existing defaults: search_knowledge.py limit=5; RetrievalQuery/API final_top_k=6.
BUDGETS = (5, 6, 20)


def diagnose(rows, qrels):
    if len(rows) != len(qrels) or {r["query_id"] for r in rows} != set(qrels):
        raise ValueError("consumer diagnostic requires the complete split")
    result = {}
    for budget in BUDGETS:
        result[budget] = {}
        for arm, field in (("baseline", "baseline_ranking"), ("candidate", "final_ranking")):
            rankings = {r["query_id"]: [h["document_id"] for h in r[field][:budget]] for r in rows}
            evaluation = evaluate_questions(qrels, rankings)
            result[budget][arm] = {
                "macro_source_recall_in_exposure": evaluation.metrics["Recall@20"],
                "document_nDCG10_in_exposure": evaluation.metrics["nDCG@10"],
                "document_MRR10_in_exposure": evaluation.metrics["MRR@10"],
                "mean_unique_sources": fmean(len(set(r)) for r in rankings.values()),
            }
    return {
        "queries": len(rows),
        "raw_chunk_budgets": result,
        "definition": "Truncate RAW children first, then deduplicate source documents",
        "scope": "Descriptive consumer-budget compatibility; no model/threshold selection",
        "additional_api_calls": 0,
        "answer_quality_evaluated": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--split", choices=("train", "test"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refusing to overwrite historical consumer diagnostic")
    exposure(args.output.parent, "CONSUMER_BUDGET_DIAGNOSTIC_LABEL_ACCESS", split=args.split)
    data = load_beir(args.data, args.split)
    rows = [json.loads(line) for line in args.run.read_text(encoding="utf-8").splitlines()]
    dump(args.output, diagnose(rows, data.qrels))


if __name__ == "__main__":
    main()
