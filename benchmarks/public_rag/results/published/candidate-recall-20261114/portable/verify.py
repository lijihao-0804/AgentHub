"""Audit the public SciFact per-query evidence using only the Python standard library."""

import argparse
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS = ("baseline", "lexical_refill4", "bge_protected_refill4")
STAGES = ("pool_positive_sources", "raw5_positive_sources", "pool_rationales", "raw5_rationales")
COHORTS = {"development": 128, "confirmation": 553}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


def verify_provenance(directory, metadata, followup, rerank, visibility):
    protocol = read(directory / "protocol.json")
    ranks = read(directory / "rerank-protocol.json")
    source = protocol["inputs"]
    beir = "data/external/rag-public-20261021/scifact/extracted/scifact/"
    expected = {
        "frozen_cohorts": hashlib.sha256(
            (directory / "cohort-freeze.json").read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest(),
        "gold_train": source[beir + "qrels/train.tsv"],
        "original_corpus": source[beir + "corpus.jsonl"],
        "source_windows_inventory": source[".scratch/rag-opt-20261021/scifact-state.json"],
        "development_top20": ranks["rankings_sha256"]["development"],
        "confirmation_top20": ranks["rankings_sha256"]["confirmation"],
        "historical_baseline_logits": ranks["history_sha256"][
            "parent-context-20261022/dev-baseline.jsonl"
        ],
        "historical_superset_logits": ranks["history_sha256"][
            "budget-diversity-20261022/screen-superset.jsonl"
        ],
        "official_train_rationales_archive": visibility["official_archive_sha256"],
        "direct_bge_scores": rerank["inference_file_sha256"],
        "derived_protected_top20": followup["protected_candidates_sha256"],
    }
    require(
        metadata["raw_source_sha256"] == expected, "source provenance differs from registration"
    )


def id_set(values):
    require(
        isinstance(values, list)
        and all(isinstance(s, str) and s.isascii() and s.isdigit() for s in values)
        and len(values) == len(set(values)),
        "expected distinct, public numeric IDs",
    )
    return frozenset(values)


def validate(row):
    require(set(row) == {"cohort", "query", "gold", "annotated", *ARMS}, "unexpected row fields")
    id_set([row["query"]])
    gold, annotated = id_set(row["gold"]), id_set(row["annotated"])
    require(gold and annotated <= gold, "invalid judged or annotated sources")
    for arm in ARMS:
        view = row[arm]
        require(isinstance(view, list) and len(view) == 4, "missing hit coverage stage")
        pool, raw5, pool_visible, raw5_visible = map(id_set, view)
        require(raw5 <= pool <= gold, "invalid top-five / pool source membership")
        require(pool_visible <= pool & annotated, "unjudged or unannotated pool rationale")
        require(raw5_visible <= raw5 & pool_visible, "invalid top-five rationale membership")
        row[arm] = (pool, raw5, pool_visible, raw5_visible)
    row.update(gold=gold, annotated=annotated)
    return row


def compare(rows, arm, stage):
    counts = dict.fromkeys(
        (
            "baseline",
            "candidate",
            "retained",
            "gained",
            "lost",
            "any_baseline",
            "any_candidate",
            "any_gained",
            "any_lost",
        ),
        0,
    )
    old_sum = new_sum = Fraction()
    gains, losses = [], []
    for r in rows:
        a, b = r["baseline"][stage], r[arm][stage]
        counts["baseline"] += len(a)
        counts["candidate"] += len(b)
        counts["retained"] += len(a & b)
        counts["gained"] += len(b - a)
        counts["lost"] += len(a - b)
        counts["any_baseline"] += bool(a)
        counts["any_candidate"] += bool(b)
        counts["any_gained"] += bool(b) and not a
        counts["any_lost"] += bool(a) and not b
        old_sum += Fraction(len(a), len(r["gold"]))
        new_sum += Fraction(len(b), len(r["gold"]))
        gains.extend((r["query"], d) for d in sorted(b - a))
        losses.extend((r["query"], d) for d in sorted(a - b))
    macros = tuple(float(s / len(rows)) for s in (old_sum, new_sum, new_sum - old_sum))
    return counts, macros, gains, losses


def verify(directory=HERE):
    directory = Path(directory)
    report_dir = directory.parent
    freeze = read(report_dir / "cohort-freeze.json")
    followup = read(report_dir / "protected-followup.json")
    visibility = read(report_dir / "rationale-visibility.json")
    rerank = read(report_dir / "rerank-report.json")
    provenance = read(directory / "provenance.json")
    verify_provenance(report_dir, provenance, followup, rerank, visibility)
    canonical = (directory / "hits.jsonl").read_bytes().replace(b"\r\n", b"\n")
    require(
        hashlib.sha256(canonical).hexdigest() == provenance["canonical_hits_sha256"],
        "hit ledger SHA-256 mismatch",
    )
    require(
        provenance["arms"] == list(ARMS) and provenance["hit_groups"] == list(STAGES),
        "incorrect ledger schema",
    )
    rows = [validate(json.loads(s)) for s in canonical.splitlines()]
    require(
        len({r["query"] for r in rows}) == len(rows) == sum(COHORTS.values()),
        "duplicate or missing query",
    )
    split = {k: [r for r in rows if r["cohort"] == k] for k in COHORTS}
    for k, subset in split.items():
        ids = [r["query"] for r in subset]
        digest = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode("ascii")).hexdigest()
        require(
            len(ids) == COHORTS[k] and digest == freeze["cohorts"][k]["ordered_ids_sha256"],
            f"{k}: frozen cohort/order differs",
        )
    require(
        set()
        .union(*(r["gold"] for r in split["development"]))
        .isdisjoint(set().union(*(r["gold"] for r in split["confirmation"]))),
        "cohorts share a positive source",
    )
    split["thirteen_previous_misses"] = [r for r in split["development"] if not r["baseline"][0]]
    require(len(split["thirteen_previous_misses"]) == 13, "prior miss cohort differs")
    result = {}
    for cohort, subset in split.items():
        reference_key = "confirmation_previously_exposed" if cohort == "confirmation" else cohort
        reference = followup["cohorts"][reference_key]
        require(reference["queries"] == len(subset), "reference cohort size differs")
        methods = {}
        for arm in ARMS:
            expected = reference["by_method"][arm]
            count_ref = expected["counts"]
            require(
                sum(len(r["gold"]) for r in subset) == count_ref["positive_pairs"],
                "positive pair denominator",
            )
            require(
                sum(len(r["annotated"]) for r in subset) == count_ref["rationale_pairs"],
                "annotated pair denominator",
            )
            stages = {}
            for i, stage in enumerate(STAGES):
                counts, macros, gains, losses = compare(subset, arm, i)
                for name, count in counts.items():
                    require(
                        count_ref[stage + "_" + name] == count, f"{cohort}/{arm}/{stage}/{name}"
                    )
                if i < 2:
                    metric = ("pool_recall", "raw5_recall")[i]
                    for label, actual in zip(
                        ("baseline", "candidate", "delta"), macros, strict=True
                    ):
                        require(
                            math.isclose(
                                expected["metrics"][metric][label], actual, abs_tol=1e-12, rel_tol=0
                            ),
                            f"{cohort}/{arm}/{metric}/{label}",
                        )
                    if arm == "lexical_refill4" and cohort in COHORTS:
                        if i == 0:
                            registered = read(report_dir / (cohort + ".json"))["methods"][arm]
                            direct = (
                                *[registered["macro_recall"][k] for k in ("baseline", "candidate")],
                                registered["delta_macro_recall"],
                            )
                        else:
                            registered = rerank["cohorts"][cohort]["results"][arm]["metrics"][
                                "raw5_source_recall"
                            ]
                            direct = tuple(
                                registered[k] for k in ("baseline", "candidate", "delta")
                            )
                        require(
                            all(
                                math.isclose(a, b, abs_tol=1e-12, rel_tol=0)
                                for a, b in zip(macros, direct, strict=True)
                            ),
                            "published original macro differs",
                        )
                elif cohort in COHORTS and arm in ("baseline", "lexical_refill4"):
                    label = "pool" if i == 2 else "top5"
                    vis = visibility["cohorts"][cohort]
                    require(
                        counts["candidate"]
                        == vis["counts"][arm][label + "_covered_rationale_pairs"],
                        "published rationale pairs differ",
                    )
                    require(
                        counts["any_candidate"]
                        == vis["counts"][arm][label + "_queries_with_any_complete_set"],
                        "published rationale queries differ",
                    )
                    if arm != "baseline":
                        for name in ("gained", "lost", "retained"):
                            require(
                                counts[name] == vis["paired"][arm][label][name],
                                "published rationale transitions differ",
                            )
                stages[stage] = {
                    **counts,
                    "gains": gains,
                    "losses": losses,
                    **({"macro": macros} if i < 2 else {}),
                }
            methods[arm] = stages
        result[cohort] = methods
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit all counts and gained/lost source IDs"
    )
    args = parser.parse_args()
    results = verify()
    if args.json:
        print(json.dumps(results, indent=2))
        return
    for phase, methods in results.items():
        print(f"\n{phase} ({COHORTS.get(phase, 13)} queries)")
        for arm, stages in methods.items():
            pool, raw, evidence20, evidence5 = (stages[k] for k in STAGES)
            print(
                f"  {arm:22} pool recall {pool['macro'][1]:.6f}, "
                f"raw5 recall {raw['macro'][1]:.6f}, "
                f"source pairs {pool['candidate']}/{raw['candidate']}, "
                f"complete rationale pairs {evidence20['candidate']}/{evidence5['candidate']}"
            )
            if arm != "baseline":
                for label, stats in (
                    ("pool sources", pool),
                    ("raw5 sources", raw),
                    ("pool evidence", evidence20),
                    ("raw5 evidence", evidence5),
                ):
                    print(
                        f"    {label}: +{stats['gained']}/-{stats['lost']} pairs; "
                        f"examples gained {stats['gains'][:1]}, lost {stats['losses'][:1]}"
                    )
    print("\nPASS: frozen cohorts, paired counts and macro recalls match the published reports.")


if __name__ == "__main__":
    main()
