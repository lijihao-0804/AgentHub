"""Run freeze, fixed development selection, and single nominated TRAIN confirmation."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.candidate_recall_methods import WordBM25, select_candidates

HERE = Path(__file__).parent


def arm_summary(rows: list, *, replicates: int, seed: int, confidence: float) -> dict:
    counts = Counter()
    multi = Counter()
    baseline_credit, candidate_credit, raw5_delta = Fraction(), Fraction(), Fraction()
    deltas_by_group: dict[str, list[float]] = {}
    for q in rows:
        gold, old, new = q["gold"], q["old"], q["new"]
        old_hit, new_hit = old & gold, new & gold
        gained, lost = new_hit - old_hit, old_hit - new_hit
        old_recall, new_recall = (
            Fraction(len(old_hit), len(gold)),
            Fraction(len(new_hit), len(gold)),
        )
        baseline_credit += old_recall
        candidate_credit += new_recall
        raw5_delta += Fraction(len(q["new5"] & gold) - len(q["old5"] & gold), len(gold))
        deltas_by_group.setdefault(q["group"], []).append(float(new_recall - old_recall))
        for tally in (counts, multi) if len(gold) > 1 else (counts,):
            tally.update(
                queries=1,
                positive_pairs=len(gold),
                baseline_pairs=len(old_hit),
                candidate_pairs=len(new_hit),
                retained_pairs=len(old_hit & new_hit),
                gained_pairs=len(gained),
                lost_pairs=len(lost),
                uncovered_in_both=len(gold - (old | new)),
                gained_pairs_outside_both_old_lanes=len(gained - q["union"]),
                baseline_partial=int(0 < len(old_hit) < len(gold)),
                candidate_partial=int(0 < len(new_hit) < len(gold)),
                baseline_any=int(bool(old_hit)),
                candidate_any=int(bool(new_hit)),
                any_retained=int(bool(old_hit) and bool(new_hit)),
                any_gained=int(not old_hit and bool(new_hit)),
                any_lost=int(bool(old_hit) and not new_hit),
                any_missing_both=int(not old_hit and not new_hit),
                all_retained=int(old_hit == gold and new_hit == gold),
                all_gained=int(old_hit != gold and new_hit == gold),
                all_lost=int(old_hit == gold and new_hit != gold),
                all_missing_both=int(old_hit != gold and new_hit != gold),
                changed_chunk_sets=int(q["changed"]),
                sources_lost_all_documents=len(old - new),
                sources_added_all_documents=len(new - old),
            )
    if not counts:
        raise ValueError("empty cohort")
    groups = sorted(deltas_by_group)
    totals = np.array([sum(deltas_by_group[g]) for g in groups])
    sizes = np.array([len(deltas_by_group[g]) for g in groups])
    draws = np.random.default_rng(seed).integers(len(groups), size=(replicates, len(groups)))
    means = totals[draws].sum(axis=1) / sizes[draws].sum(axis=1)
    tail = (1 - confidence) / 2
    n = counts["queries"]
    delta = (candidate_credit - baseline_credit) / n
    return {
        "queries": n,
        "components": len(groups),
        "macro_recall": {
            "baseline": float(baseline_credit / n),
            "candidate": float(candidate_credit / n),
        },
        "delta_macro_recall": float(delta),
        "delta_macro_exact": str(delta),
        "paired_cluster_interval_97_5": np.quantile(means, [tail, 1 - tail]).tolist(),
        "retention_among_baseline_positive_pairs": (
            counts["retained_pairs"] / counts["baseline_pairs"]
            if counts["baseline_pairs"]
            else None
        ),
        "raw5_macro_delta_before_rerank": float(raw5_delta / n),
        "all_cases": dict(counts),
        "multiple_positive_cases": dict(multi),
    }


def choose(families: dict, stats: dict, raw5_margin: float) -> dict:
    choice = {}
    for family, methods in families.items():
        eligible = [
            m
            for m in methods
            if stats[m]["all_cases"]["gained_pairs"] >= 1
            and stats[m]["all_cases"]["lost_pairs"] == 0
            and stats[m]["raw5_macro_delta_before_rerank"] >= -raw5_margin
        ]
        choice[family] = max(
            eligible,
            key=lambda m: (
                Fraction(stats[m]["delta_macro_exact"]),
                stats[m]["all_cases"]["any_gained"],
            ),
            default=None,
        )
    return choice


def gates(stats: dict, rules: dict) -> dict:
    return {
        "recovered_complete_miss": stats["all_cases"]["any_gained"] > 0,
        "retained_every_baseline_positive_pair": stats["all_cases"]["lost_pairs"] == 0,
        "minimum_macro_recall_gain": stats["delta_macro_recall"] >= rules["min_macro_gain"],
        "positive_multiplicity_adjusted_lower_bound": stats["paired_cluster_interval_97_5"][0] > 0,
        "raw_first_five_margin": stats["raw5_macro_delta_before_rerank"] >= -rules["raw5_margin"],
    }


def run_arm_comparison(ids: list[str], methods: list[str], groups: dict, directory: Path) -> dict:
    protocol = json.loads(data.PROTOCOL.read_bytes())
    history = data.historical_rows(ids)
    labels = data.train_qrels(set(ids))
    questions = data.queries(ids)
    index = None
    if any(m.startswith("lexical_") for m in methods):
        index = WordBM25(data.snapshot_chunks())
    rows = {m: [] for m in methods}
    with (directory / "rankings.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for qid in ids:
            stages = history[qid]
            dense, sparse = (stages[route]["ranking"] for route in ("dense", "sparse"))
            if len(dense) != 30 or len(sparse) != 30:
                raise ValueError(f"historical lane depth differs for {qid}")
            original = select_candidates("baseline", dense, sparse)
            if (
                original != stages["selection"]["ranking"]
                or original != stages["fusion"]["ranking"]
            ):
                raise ValueError(f"baseline differs from archived candidate order/scores for {qid}")
            union = {h["document_id"] for h in dense + sparse}
            old = {h["document_id"] for h in original}
            gold = {doc for doc, grade in labels[qid].items() if grade > 0}
            if not gold or len(original) != 20:
                raise ValueError("empty judgments or incomplete baseline")
            lexical = index.query(questions[qid]) if index is not None else []
            rankings = {"query_id": qid, "baseline": original}
            for method in methods:
                selected = select_candidates(method, dense, sparse, lexical)
                if len(selected) != 20 or len({h["chunk_id"] for h in selected}) != 20:
                    raise ValueError(f"{method}: candidate budget differs for {qid}")
                rankings[method] = selected
                rows[method].append(
                    {
                        "group": groups[qid],
                        "gold": gold,
                        "union": union,
                        "old": old,
                        "new": {h["document_id"] for h in selected},
                        "old5": {h["document_id"] for h in original[:5]},
                        "new5": {h["document_id"] for h in selected[:5]},
                        "changed": {h["chunk_id"] for h in original}
                        != {h["chunk_id"] for h in selected},
                    }
                )
            stream.write(json.dumps(rankings, separators=(",", ":")) + "\n")
    boot = protocol["confirmation"]["bootstrap"]
    return {
        method: arm_summary(
            cases,
            replicates=boot["replicates"],
            seed=boot["seed"],
            confidence=boot["two_sided_confidence"],
        )
        for method, cases in rows.items()
    }


def source_hashes() -> dict:
    return {
        name: data.sha256(HERE / name)
        for name in (
            "candidate_recall_methods.py",
            "candidate_recall_data.py",
            "candidate_recall_study.py",
        )
    }


def study(phase: str, directory: Path) -> dict:
    if phase == "freeze":
        return data.freeze(directory)
    if phase not in ("development", "confirmation"):
        raise ValueError("unknown phase")
    cohorts, groups = data.checked_cohorts(directory)
    protocol = json.loads(data.PROTOCOL.read_bytes())
    families = protocol["families"]
    ids = cohorts[phase]
    if phase == "development":
        methods = [m for family in families.values() for m in family]
    else:
        lock = json.loads((directory / "selection.json").read_bytes())
        if (
            lock["protocol_sha256"] != data.sha256(data.PROTOCOL)
            or lock["source_sha256"] != source_hashes()
            or lock["development_report_sha256"]
            != data.sha256(directory / "development/report.json")
            or lock["confirmation_ids_sha256"] != data.fingerprint(ids)
        ):
            raise ValueError("development selection lock no longer matches evidence")
        methods = [m for m in lock["nominees"].values() if m is not None]
    output = directory / phase
    output.mkdir(exist_ok=False)
    stats = run_arm_comparison(ids, methods, groups, output) if methods else {}
    report = {
        "phase": phase,
        "time_utc": datetime.now(UTC).isoformat(),
        "protocol_sha256": data.sha256(data.PROTOCOL),
        "source_sha256": source_hashes(),
        "ordered_ids_sha256": data.fingerprint(ids),
        "n_registered": len(ids),
        "n_evaluated": len(ids) if methods else 0,
        "methods": stats,
        "rankings_sha256": data.sha256(output / "rankings.jsonl") if methods else None,
    }
    if phase == "development":
        nominees = choose(families, stats, protocol["selection"]["raw5_margin"])
        report["nominees"] = nominees
    else:
        report["nominees"] = lock["nominees"]
        report["candidate_recall_gates"] = {
            method: gates(result, protocol["confirmation"]) for method, result in stats.items()
        }
    data.save(output / "report.json", report)
    if phase == "development":
        data.save(
            directory / "selection.json",
            {
                "frozen_at_utc": datetime.now(UTC).isoformat(),
                "nominees": nominees,
                "protocol_sha256": data.sha256(data.PROTOCOL),
                "source_sha256": source_hashes(),
                "development_report_sha256": data.sha256(output / "report.json"),
                "confirmation_ids_sha256": data.fingerprint(cohorts["confirmation"]),
            },
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "development", "confirmation"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = study(args.phase, args.output)
    print(
        json.dumps(
            {k: result[k] for k in ("phase", "nominees", "candidate_recall_gates") if k in result}
        )
    )


if __name__ == "__main__":
    main()
