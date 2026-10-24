"""Posthoc answer-value/format diagnostics; official scores are never changed.

A leading Yes/No token matching a public reference is NOT an entailment test.
Source support and factual correctness remain UNKNOWN without separate review.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from benchmarks.public_rag.consumer_answer_metrics import answer_metrics


def polarity_view(result, references):
    polar = {ref.strip().casefold() for ref in references}
    if not polar or not polar <= {"yes", "no"}:
        return {"status": "NOT_A_REGISTERED_POLAR_REFERENCE"}
    if len(polar) != 1:
        return {"status": "CONTRADICTORY_PUBLIC_REFERENCES", "reference_match": None}
    if result["status"] != "VALID":
        return {"status": "UNKNOWN_RESULT", "reference_match": None}
    if result["unanswerable"]:
        return {"status": "REFUSED", "reference_match": None}
    raw = result["answer"].strip()
    leading = re.match(r"^(yes|no)\b", raw, re.I)
    token = leading.group(1).casefold() if leading else None
    return {
        "status": "VALUE_AND_FORMAT_DIAGNOSTIC",
        "reference_match": token in polar,
        "leading_token": token,
        "bare_value_format": raw.casefold() in {"yes", "no"},
        "official": answer_metrics(raw, references),
        "semantic_support": "UNKNOWN",
        "factual_correctness": "UNKNOWN",
    }


def audit(dataset, results):
    arms = ("deepseek_high", "glm_high")
    rows = {a: {r["query_id"]: r for r in results[a]["cases"]} for a in arms}
    cases, full_delta, polar_delta = [], 0.0, 0.0
    denominator = len(dataset["cases"])
    for case in dataset["cases"]:
        pair = {a: rows[a][case["query_id"]]["conditions"]["sufficient"] for a in arms}
        if any(r["status"] != "VALID" for r in pair.values()):
            raise ValueError("posthoc gap decomposition requires the complete sufficient condition")
        values = {
            a: answer_metrics(pair[a]["answer"], case["answer_references"])["token_f1"]
            for a in arms
        }
        delta = values[arms[1]] - values[arms[0]]
        full_delta += delta / denominator
        if set(case["answer_types"]) != {"yes_no"}:
            continue
        views = {a: polarity_view(pair[a], case["answer_references"]) for a in arms}
        cases.append(
            {
                "query_id": case["query_id"],
                "paper_id": case["paper_id"],
                "question": case["question"],
                "arms": views,
                "macro_f1_contribution_pp": 100 * delta / denominator,
            }
        )
        polar_delta += delta / denominator
    return {
        "scope": "Posthoc attribution of the complete sufficient-evidence development condition; "
        "not a new benchmark or replacement score.",
        "registered_queries": denominator,
        "polarity_cases": len(cases),
        "original_macro_f1_delta_pp": 100 * full_delta,
        "polarity_cases_macro_contribution_pp": 100 * polar_delta,
        "signed_share_of_observed_lexical_delta": polar_delta / full_delta if full_delta else None,
        "warning": "This is NOT a fraction of factual errors or all RAG errors. "
        "Value correctness and compliance with the no-explanation instruction differ. "
        "Do not treat a matched leading token as source entailment.",
        "official_scores_changed": False,
        "new_model_calls": 0,
        "promotion": False,
        "cases": cases,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite a recorded diagnostic")
    report = audit(
        json.loads(args.dataset.read_text(encoding="utf-8")),
        json.loads(args.run.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))
