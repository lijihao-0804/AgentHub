"""Summarize the frozen paired sample with separate language, grounding and stage denominators."""

import argparse
import json
from collections import Counter
from decimal import Decimal
from math import comb
from pathlib import Path

from benchmarks.retrieval.experiments.first_refusal_100.summarize import fraction
from packages.core.canonical.json_parse import load_unique_json

ARMS = ("baseline", "candidate")
PHASES = ("first", "final")
STAGES = ("candidate", "reranked", "presented")


def rate(items, flag):
    return fraction(sum(item["scores"][flag] is True for item in items), len(items))


def arm_metrics(rows, phase):
    answers = [r[phase] for r in rows if r["answerable"]]
    negatives = [r[phase] for r in rows if not r["answerable"]]
    all_items = answers + negatives
    cited = [i for i in all_items if i["state"] == "answer" and i["cited_chunks"]]
    supported = [r for r in rows if r["answerable"] and r["retrieval"]["presented"]["gold_covered"]]
    languages = Counter(i["scores"]["language_match"] for i in all_items)
    return {
        "strict_all": rate(all_items, "joint_correct"),
        "semantic_and_citation_all": rate(all_items, "semantic_and_citation_correct"),
        "value_all": rate(all_items, "answer_correct"),
        "citation_all": rate(all_items, "citation_correct"),
        "positive_value": rate(answers, "answer_correct"),
        "positive_value_with_language": rate(answers, "value_with_language"),
        "positive_supported": rate(answers, "semantic_and_citation_correct"),
        "positive_strict": rate(answers, "joint_correct"),
        "correct_refusal": rate(negatives, "joint_correct"),
        "semantic_refusal": rate(negatives, "semantic_and_citation_correct"),
        "false_refusals": rate(answers, "wrong_refusal"),
        "unsafe_answers": rate(negatives, "unsafe_answer"),
        "refusals_all": fraction(sum(i["state"] == "refusal" for i in all_items), len(all_items)),
        "cited_answer_support": rate(cited, "citation_correct"),
        "cited_answer_support_with_language": rate(cited, "citation_with_language"),
        "language_match": rate(all_items, "language_match"),
        "language_mismatches": languages[False],
        "language_unassessable": languages[None],
        "language_mismatch_ids": [
            r["id"] for r in rows if r[phase]["scores"]["language_match"] is False
        ],
        "unresolved_review_ids": [
            r["id"] for r in rows if r[phase].get("semantic_review", {}).get("unresolved")
        ],
        "invalid_reasons": dict(
            Counter(i["invalid_reason"] for i in all_items if i["invalid_reason"])
        ),
        "states": dict(Counter(i["state"] for i in all_items)),
        "gold_presented_but_refused": {
            **fraction(sum(r[phase]["state"] == "refusal" for r in supported), len(supported)),
            "ids": [r["id"] for r in supported if r[phase]["state"] == "refusal"],
        },
    }


def by_stage(rows):
    positives = [r for r in rows if r["answerable"]]
    result = {}
    for stage in STAGES:
        missing = [r["id"] for r in positives if not r["retrieval"][stage]["gold_covered"]]
        result[stage] = {
            "annotated_support_coverage": fraction(len(positives) - len(missing), len(positives)),
            "missing_support_ids": missing,
            "chunk_count_distribution": dict(
                Counter(r["retrieval"][stage]["chunks"] for r in rows)
            ),
        }
    return result


def change(pairs, phase, flag, disposition=None):
    eligible = [
        p for p in pairs if disposition is None or p["baseline"]["answerable"] == disposition
    ]
    improved, regressed = [], []
    for pair in eligible:
        passed = [pair[arm][phase]["scores"][flag] is True for arm in ARMS]
        if passed[0] != passed[1]:
            (improved if passed[1] else regressed).append(pair["baseline"]["id"])
    n = len(improved) + len(regressed)
    tail = sum(comb(n, k) for k in range(min(len(improved), len(regressed)) + 1))
    return {
        "improved": improved,
        "regressed": regressed,
        "net_count": len(improved) - len(regressed),
        "unchanged_count": len(eligible) - n,
        "paired_exact_two_sided_p": min(1.0, 2 * tail / 2**n),
    }


def cohort(pairs):
    arms = {}
    for arm in ARMS:
        side = [p[arm] for p in pairs]
        arms[arm] = {
            **{phase: arm_metrics(side, phase) for phase in PHASES},
            "first_http_requests": len(side),
            "confirmed_calls": sum(r["attempts"] for r in side),
            "corrections": sum(r["attempts"] - 1 for r in side),
            "estimated_cny": str(sum(Decimal(r["estimated_cny"]) for r in side)),
            "http_status": dict(Counter(r["http"] for r in side)),
        }
    endpoints = (
        ("strict", "joint_correct", None),
        ("semantic_and_citation", "semantic_and_citation_correct", None),
        ("language", "language_match", None),
        ("value", "answer_correct", None),
        ("citation", "citation_correct", None),
        ("positive_value", "answer_correct", True),
        ("positive_supported", "semantic_and_citation_correct", True),
        ("positive_strict", "joint_correct", True),
        ("correct_refusal", "joint_correct", False),
    )
    return {
        "questions": len(pairs),
        "arms": arms,
        "retrieval": by_stage([p["baseline"] for p in pairs]),
        "paired_changes": {
            phase: {
                label: change(pairs, phase, flag, positive) for label, flag, positive in endpoints
            }
            for phase in PHASES
        },
    }


def summarize(rows, *, smoke=False):
    indexed = {}
    for row in rows:
        arms = indexed.setdefault(row["id"], {})
        if row["arm"] not in ARMS or row["arm"] in arms or row["attempts"] not in (1, 2):
            raise ValueError("Missing or duplicate arm or unexpected attempt count.")
        arms[row["arm"]] = row
        coverage_type = bool if row["answerable"] else type(None)
        if (
            type(row["answerable"]) is not bool
            or row["language"] not in ("zh", "en")
            or set(row["retrieval"]) != set(STAGES)
            or any(type(v["gold_covered"]) is not coverage_type for v in row["retrieval"].values())
        ):
            raise ValueError("Invalid language, disposition or annotated-support denominator.")
        for phase in PHASES:
            scores = row[phase]["scores"]
            semantic = scores["answer_correct"] and scores["citation_correct"]
            if (
                semantic != scores["semantic_and_citation_correct"]
                or scores["joint_correct"] != (semantic and scores["language_match"] is True)
                or row[phase]["presented_evidence_sha256"] != row["evidence_sha256"]
                or row[phase]["wire_attempt"] != (1 if phase == "first" else row["attempts"])
            ):
                raise ValueError(
                    "A reviewed state, evidence denominator or score binding is inconsistent."
                )
    pairs = [indexed[qid] for qid in sorted(indexed)]
    for p in pairs:
        if set(p) != set(ARMS) or any(
            p["baseline"][k] != p["candidate"][k]
            for k in (
                "language",
                "answerable",
                "evidence_sha256",
                "retrieval_order_sha256",
                "retrieval",
            )
        ):
            raise ValueError("Only complete pairs with identical actual evidence are comparable.")
    counts = Counter((p["baseline"]["language"], p["baseline"]["answerable"]) for p in pairs)
    if not smoke and counts != {(lang, pos): 25 for lang in ("zh", "en") for pos in (True, False)}:
        raise ValueError("The report requires the frozen 100-question sample.")
    return {
        "schema": "bilingual_refusal_100_paired_metrics_v1",
        **cohort(pairs),
        "by_language": {
            lang: cohort([p for p in pairs if p["baseline"]["language"] == lang])
            for lang in ("en", "zh")
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--offline-smoke", action="store_true", help="Permit the small synthetic fixture set."
    )
    args = parser.parse_args()
    records = [load_unique_json(line.decode()) for line in args.records.read_bytes().splitlines()]
    summary = summarize(records, smoke=args.offline_smoke)
    with args.output.open("x", encoding="utf8", newline="\n") as dest:
        json.dump(summary, dest, indent=2, ensure_ascii=False)
        dest.write("\n")
