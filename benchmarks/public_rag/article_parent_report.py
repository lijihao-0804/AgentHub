"""Score labeled source and sentence visibility only after hypothesis-blind pool selection."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.article_parent_run import (
    CACHE,
    METHODS,
    PUBLIC,
    SCRATCH,
    SIX,
    STUDY,
)
from benchmarks.public_rag.candidate_recall_rationales import complete_rationale
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, rank, verified_inputs
from benchmarks.public_rag.scifact_recall_forensics import archive_records, sentence_ranges
from benchmarks.public_rag.scifact_recall_index import frozen_source
from benchmarks.public_rag.scoring import evaluate_questions

STAGES = ("source20", "source5", "evidence20", "evidence5")


def source_sha256_lf(path):
    """Hash text source with Git LF line endings; measured inference files use raw bytes."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def measures(pool, reranked, chunks, gold, evidence, spans):
    state = {}
    for n, hits in ((20, pool), (5, reranked[:5])):
        state[f"source{n}"] = sorted({h["document_id"] for h in hits} & gold)
        visible = []
        for d, groups in evidence.items():
            windows = [
                (chunks[h["chunk_id"]]["char_start"], chunks[h["chunk_id"]]["char_end"])
                for h in hits
                if h["document_id"] == d
            ]
            if complete_rationale(groups, spans[d], windows):
                visible.append(d)
        state[f"evidence{n}"] = sorted(visible)
    return state


def aggregate(ids, cases, groups):
    report = {}
    cluster_ids = sorted({groups[q] for q in ids})
    group_index = np.asarray([cluster_ids.index(groups[q]) for q in ids])
    draws = np.random.default_rng(20261114).integers(
        len(cluster_ids), size=(10000, len(cluster_ids))
    )
    for method in ("baseline", *METHODS):
        counts = {k: Counter() for k in STAGES}
        costs, metrics = Counter(), Counter()
        for q in ids:
            case = cases[q]
            base = case["methods"]["baseline"]
            arm = case["methods"][method]
            for key in STAGES:
                old, new = set(base[key]), set(arm[key])
                eligible = len(case["annotated"] if key.startswith("evidence") else case["gold"])
                counts[key].update(
                    eligible_pairs=eligible,
                    eligible_queries=bool(eligible),
                    visible_pairs=len(new),
                    retained=len(old & new),
                    gained=len(new - old),
                    lost=len(old - new),
                    any_visible=bool(new),
                    any_gained=bool(new) and not old,
                    any_lost=bool(old) and not new,
                )
                if eligible:
                    metrics[key] += len(new) / eligible
            costs.update(
                added_chunks=arm["added_chunks"],
                added_from_outside_historical_union=arm["new_bge_pairs"],
                queries_changed=arm["added_chunks"] > 0,
            )
            for key, val in arm["document_metrics"].items():
                metrics[key] += val
        report[method] = {
            "stages": counts,
            "cost": {**costs, "bge_pairs_if_served": 20 * len(ids)},
            "macro": {
                k: v / (counts[k]["eligible_queries"] if k in STAGES else len(ids))
                for k, v in metrics.items()
            },
        }
        intervals = {}
        for key in STAGES:
            differences, eligible = [], []
            for q in ids:
                case = cases[q]
                denom = len(case["annotated"] if key.startswith("evidence") else case["gold"])
                eligible.append(int(denom > 0))
                difference = len(case["methods"][method][key]) - len(
                    case["methods"]["baseline"][key]
                )
                differences.append(difference / denom if denom else 0.0)
            sizes = np.bincount(group_index, weights=eligible)[draws].sum(axis=1)
            changes = np.bincount(group_index, weights=differences)[draws].sum(axis=1)
            intervals[key] = {
                "delta": sum(differences) / sum(eligible),
                "descriptive_group_bootstrap_95": np.quantile(
                    changes[sizes > 0] / sizes[sizes > 0], [0.025, 0.975]
                ).tolist(),
            }
        report[method]["paired_macro"] = intervals
    return {"queries": len(ids), "methods": report}


def run(work, output):
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("cases.json", "six.json", "report.json")):
        raise FileExistsError("use a fresh output directory to preserve existing results")
    _, cohorts, groups, _, cache, _ = verified_inputs(STUDY)
    source = frozen_source(json.loads(MODEL_PROTOCOL.read_bytes()))
    claims, official = archive_records(SCRATCH / "scifact-rationale-source-20261114/data.tar.gz")
    chunks = {r["chunk_id"]: r for r in data.jsonl(CACHE / "manifest.jsonl")}
    inference = json.loads((work / "inference.json").read_bytes())
    for label, file in (
        ("candidate_sha256", "candidates.jsonl"),
        ("score_sha256", "scores.jsonl"),
        ("standalone_sha256", "standalone.json"),
    ):
        if data.sha256(work / file) != inference[label]:
            raise ValueError("scoring evidence fingerprint changed")
    new = {r["query_id"]: r["scores"] for r in data.jsonl(work / "scores.jsonl")}
    standalone = json.loads((work / "standalone.json").read_bytes())
    six_details, cases = {}, {}
    for r in data.jsonl(work / "candidates.jsonl"):
        q = r["query_id"]
        gold = {d for d, g in source.qrels[q].items() if g > 0}
        evidence = {d: e for d, e in claims[q]["evidence"].items() if e}
        spans = {d: sentence_ranges(source.corpus[d], official[d]) for d in evidence}
        states = {}
        base_ids = {h["chunk_id"] for h in r["pools"]["baseline"]}
        score = {**cache[q], **new[q]}
        for m, hits in r["pools"].items():
            assert len(hits) == len({h["chunk_id"] for h in hits}) == 20
            assert all(chunks[h["chunk_id"]]["document_id"] == h["document_id"] for h in hits)
            ranked = rank(hits, score)
            state = measures(hits, ranked, chunks, gold, evidence, spans)
            metrics = evaluate_questions(
                {q: source.qrels[q]}, {q: [h["document_id"] for h in ranked]}
            ).metrics
            state["document_metrics"] = {k: metrics[k] for k in ("nDCG@10", "Recall@5", "MRR@5")}
            state["added_chunks"] = len({h["chunk_id"] for h in hits} - base_ids)
            state["new_bge_pairs"] = sum(h["chunk_id"] not in cache[q] for h in hits)
            states[m] = state
        cases[q] = {"gold": sorted(gold), "annotated": sorted(evidence), "methods": states}
        if q in SIX:
            details = {}
            for m, lanes in r["parent_rankings"].items():
                details[m] = {
                    d: {k: (lane.index(d) + 1 if d in lane else None) for k, lane in lanes.items()}
                    for d in sorted(gold)
                }
            incumbents = {h["document_id"] for h in r["pools"]["baseline"]}
            admission = {}
            for method, lanes in r["parent_rankings"].items():
                novel = [d for d in lanes["fusion"][:20] if d not in incumbents]
                admission[method] = {
                    "novel_parents_in_first20": len(novel),
                    "slots_filled": states[method]["added_chunks"],
                    "gold_rank_among_novel_top20": {
                        d: (novel.index(d) + 1 if d in novel else None) for d in sorted(gold)
                    },
                }
            standalone_results = {}
            for m, pool in standalone[q].items():
                ranked = rank(pool, score)
                standalone_results[m] = {
                    **measures(pool, ranked, chunks, gold, evidence, spans),
                    "gold_rerank_positions": {
                        h["document_id"]: i
                        for i, h in enumerate(ranked, 1)
                        if h["document_id"] in gold
                    },
                    "ordered_chunk_ids_after_rerank": [h["chunk_id"] for h in ranked],
                }
            six_details[q] = {
                **cases[q],
                "ranks": details,
                "admission": admission,
                "standalone_diagnostic": standalone_results,
            }
    if set(six_details) != set(SIX) or set(cases) != set(
        cohorts["development"] + cohorts["confirmation"]
    ):
        raise ValueError("partial population")
    forensics = json.loads((data.PUBLIC / "forensics-cases.json").read_bytes())
    if {r["query_id"] for r in forensics if r["observed_loss"] == "both_branches_top30"} != set(
        SIX
    ):
        raise ValueError("original branch misses differ")
    data.save(output / "cases.json", cases)
    data.save(output / "six.json", six_details)
    data.save(
        output / "report.json",
        {
            "protocol_sha256_lf": source_sha256_lf(PUBLIC / "protocol.json"),
            "case_sha256": data.sha256(output / "cases.json"),
            "standalone_protocol_sha256_lf": source_sha256_lf(PUBLIC / "standalone-addendum.json"),
            "six_sha256": data.sha256(output / "six.json"),
            "input_vector_manifests": {
                f: data.sha256(CACHE / f) for f in ("chunks-ready.json", "complete.json")
            },
            "index_bytes": {
                p.name: p.stat().st_size
                for p in (
                    CACHE / "parent-dense.npy",
                    CACHE / "parent-sparse.npz",
                    work / "title-dense.npy",
                    work / "title-sparse.npz",
                )
            },
            "title_index": json.loads((work / "title-index.json").read_bytes()),
            "measured_inference": inference,
            "cohorts": {
                n: aggregate(ids, cases, groups)
                for n, ids in (
                    ("original_six", SIX),
                    ("development", cohorts["development"]),
                    ("previously_exposed_553", cohorts["confirmation"]),
                )
            },
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.work, args.output)
