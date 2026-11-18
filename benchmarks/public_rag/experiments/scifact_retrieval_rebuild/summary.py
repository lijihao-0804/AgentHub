"""Verify the public question records and reproduce all paired counts with the standard library."""

import argparse
import hashlib
import json
from collections import Counter
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTOCOL = HERE / "protocol.json"
HISTORY = HERE.parents[1] / "results/published/candidate-recall-20261114/portable"
STAGES = ("pool_sources", "top5_sources", "pool_evidence", "top5_evidence")
HISTORICAL_ARMS = {"rrf20": "baseline", "bm25_refill20": "lexical_refill4"}


def read(path):
    return json.loads(path.read_bytes())


def text_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def rows(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def stage_totals(questions, arm):
    result = {}
    for i, name in enumerate(STAGES):
        denominator = "gold" if i < 2 else "annotated"
        eligible = [r for r in questions if r[denominator]]
        recall = sum(Fraction(len(r[arm][i]), len(r[denominator])) for r in eligible)
        result[name] = {
            "pairs": sum(len(r[arm][i]) for r in questions),
            "queries_with_hit": sum(bool(r[arm][i]) for r in questions),
            "macro_recall_over_eligible_queries": float(recall / len(eligible)),
        }
    return result


def changed(questions, before, after):
    result = {}
    for i, name in enumerate(STAGES):
        gained, lost, any_gained, any_lost = [], [], [], []
        for row in questions:
            q = row["query"]
            a, b = set(row[before][i]), set(row[after][i])
            gained.extend([q, d] for d in sorted(b - a))
            lost.extend([q, d] for d in sorted(a - b))
            if b and not a:
                any_gained.append(q)
            if a and not b:
                any_lost.append(q)
        result[name] = {
            "gained_pairs": gained,
            "lost_pairs": lost,
            "zero_to_hit_queries": any_gained,
            "hit_to_zero_queries": any_lost,
        }
    return result


def build_summary(directory: Path):
    config = read(PROTOCOL)
    provenance = read(directory / "provenance.json")
    if (
        text_hash(directory / "questions.jsonl") != provenance["questions_sha256"]
        or text_hash(PROTOCOL) != provenance["protocol_sha256"]
        or text_hash(HISTORY / "hits.jsonl")
        != read(HISTORY / "provenance.json")["canonical_hits_sha256"]
    ):
        raise ValueError("public protocol, current ledger or historical ledger changed")
    questions = rows(directory / "questions.jsonl")
    historical = [r for r in rows(HISTORY / "hits.jsonl") if r["cohort"] == config["cohort"]]
    ids = [r["query"] for r in questions]
    digest = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()
    if (
        len(set(ids)) != len(ids)
        or len(ids) != config["queries"]
        or ids != [r["query"] for r in historical]
        or digest != config["ordered_query_ids_sha256"]
    ):
        raise ValueError("queries or order differ from the frozen historical cohort")
    arms = list(config["arms"])
    mismatches = {arm: {s: [] for s in STAGES} for arm in HISTORICAL_ARMS}
    for row, old in zip(questions, historical, strict=True):
        q = row["query"]
        gold, annotated = set(row["gold"]), set(row["annotated"])
        if gold != set(old["gold"]) or annotated != set(old["annotated"]):
            raise ValueError(f"changed denominators for {q}")
        if not 0 <= row["additional_chunks"] <= 4:
            raise ValueError(f"invalid expansion depth for {q}")
        for arm in arms:
            values = row[arm]
            if len(values) != len(STAGES) or any(len(v) != len(set(v)) for v in values):
                raise ValueError(f"invalid or repeated source for {q}, {arm}")
            pool, top, pool_ev, top_ev = map(set, values)
            if not (
                top <= pool <= gold and top_ev <= top & pool_ev and pool_ev <= pool & annotated
            ):
                raise ValueError(f"inconsistent source/evidence membership for {q}, {arm}")
            if arm in HISTORICAL_ARMS:
                previous = old[HISTORICAL_ARMS[arm]]
                for i, stage in enumerate(STAGES):
                    if set(values[i]) != set(previous[i]):
                        mismatches[arm][stage].append(q)
        for arm in ("rrf_expand", "bm25_append"):
            for i in (0, 2):
                if not set(row["rrf20"][i]) <= set(row[arm][i]):
                    raise ValueError(f"keeping the first 20 lost a pool hit for {q}, {arm}")
    agreements = {
        stage: {
            kind + "_mismatch_queries": [
                row["query"]
                for row in questions
                if not row["historical_chunk_agreement"][stage][kind]
            ]
            for kind in ("order", "members")
        }
        for stage in ("dense30", "sparse30", "rrf20", "rrf_top5")
    }
    count = len(questions)
    depths = Counter(r["additional_chunks"] for r in questions)
    baseline_depth = count * config["pool"]
    expanded_depth = baseline_depth + sum(k * v for k, v in depths.items())
    return {
        "questions": count,
        "positive_pairs": sum(len(r["gold"]) for r in questions),
        "annotated_questions": sum(bool(r["annotated"]) for r in questions),
        "annotated_pairs": sum(len(r["annotated"]) for r in questions),
        "source_components_in_frozen_cohort": read(HISTORY.parent / "cohort-freeze.json")[
            "cohorts"
        ][config["cohort"]]["source_components"],
        "stage_totals": {arm: stage_totals(questions, arm) for arm in arms},
        "historical_stage_totals": {
            arm: stage_totals(historical, historical_arm)
            for arm, historical_arm in HISTORICAL_ARMS.items()
        },
        "historical_source_or_evidence_mismatch_queries": mismatches,
        "historical_chunk_agreement": agreements,
        "paired_changes": {
            before + "_to_" + after: changed(questions, before, after)
            for before, after in (
                ("rrf20", "bm25_refill20"),
                ("rrf20", "rrf_expand"),
                ("rrf20", "bm25_append"),
                ("rrf_expand", "bm25_append"),
            )
        },
        "extra_depth_distribution": {str(k): depths[k] for k in sorted(depths)},
        "logical_scored_pairs": {
            "rrf20": baseline_depth,
            "bm25_refill20": baseline_depth,
            "rrf_expand": expanded_depth,
            "bm25_append": expanded_depth,
        },
        "fresh_scored_pairs_after_identical_input_reuse": sum(
            r["fresh_scored_pairs"] for r in questions
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=HERE / "results")
    args = parser.parse_args()
    regenerated = build_summary(args.directory)
    if read(args.directory / "summary.json") != regenerated:
        raise ValueError("registered summary differs from per-query reconstruction")
    print(json.dumps(regenerated["stage_totals"], indent=2))
