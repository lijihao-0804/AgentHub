"""One pre-specified post-exposure study of BGE-protected lexical replacement."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.candidate_recall_rationales import complete_rationale
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, rank, verified_inputs
from benchmarks.public_rag.scifact_recall_forensics import archive_records, sentence_ranges
from benchmarks.public_rag.scifact_recall_index import chunk_manifest, frozen_source
from benchmarks.public_rag.scoring import evaluate_questions

PROTOCOL = data.PUBLIC / "protected-followup-protocol.json"
METHOD = "bge_protected_refill4"
METHODS = ("baseline", "lexical_refill4", METHOD)


def protected_refill(baseline, original_logits, lexical):
    """Select 20 from the original pool and a lexical top20 or its verified novelty prefix."""
    ranked = rank(baseline, original_logits)
    protected = {h["chunk_id"] for h in baseline[:8] + ranked[:10]}
    for view in (baseline, ranked):
        first = {}
        for h in view:
            first.setdefault(h["document_id"], h["chunk_id"])
        protected.update(first.values())
    replace = [h["chunk_id"] for h in reversed(baseline) if h["chunk_id"] not in protected][:4]
    if not replace:
        return list(baseline)
    docs = {h["document_id"] for h in baseline}
    extra = []
    for h in lexical:
        if h["document_id"] in docs:
            continue
        docs.add(h["document_id"])
        extra.append({**h, "score": baseline[-1]["score"] / (2 + len(extra))})
        if len(extra) == len(replace):
            break
    removed = set(replace[: len(extra)])
    return [h for h in baseline if h["chunk_id"] not in removed] + extra


def paired_delta(base, candidate, draws, group_index):
    a, b = np.asarray(base), np.asarray(candidate)
    diff = b - a
    sizes = np.bincount(group_index)[draws].sum(axis=1)
    samples = np.bincount(group_index, weights=diff)[draws].sum(axis=1) / sizes
    return {
        "baseline": a.mean().item(),
        "candidate": b.mean().item(),
        "delta": diff.mean().item(),
        "descriptive_ci97_5": np.quantile(samples, [0.0125, 0.9875]).tolist(),
    }


def summary(ids, states, groups):
    gids = sorted({groups[q] for q in ids})
    indices = np.asarray([gids.index(groups[q]) for q in ids])
    draws = np.random.default_rng(20261114).integers(len(gids), size=(10000, len(gids)))
    result = {}
    for method in METHODS:
        tallies = Counter()
        metrics = {}
        for m in (
            "pool_recall",
            "raw5_recall",
            "document_ndcg10",
            "document_recall5",
            "document_mrr5",
        ):
            metrics[m] = paired_delta(
                [states[q]["baseline"][m] for q in ids],
                [states[q][method][m] for q in ids],
                draws,
                indices,
            )
        for q in ids:
            a, b = states[q]["baseline"], states[q][method]
            for key in (
                "pool_positive_sources",
                "raw5_positive_sources",
                "pool_rationales",
                "raw5_rationales",
            ):
                old, new = a[key], b[key]
                for label, count in (
                    ("baseline", len(old)),
                    ("candidate", len(new)),
                    ("gained", len(new - old)),
                    ("lost", len(old - new)),
                    ("retained", len(old & new)),
                    ("any_baseline", bool(old)),
                    ("any_candidate", bool(new)),
                    ("any_gained", bool(new) and not old),
                    ("any_lost", bool(old) and not new),
                ):
                    tallies[key + "_" + label] += count
            tallies["positive_pairs"] += b["gold_count"]
            tallies["rationale_pairs"] += b["rationale_count"]
            tallies["extra_bge_pairs"] += b["extra_bge_pairs"]
            tallies["queries_with_extra_bge_call"] += b["extra_bge_pairs"] > 0
            tallies["chosen_new_chunks"] += b["chosen_new_chunks"]
            tallies["discarded_baseline_best_gold_chunks"] += b[
                "discarded_baseline_best_gold_chunks"
            ]
        result[method] = {
            "metrics": metrics,
            "counts": dict(tallies),
            "bge_pairs": 20 * len(ids) + tallies["extra_bge_pairs"],
        }
    return {"queries": len(ids), "groups": len(gids), "by_method": result}


def run(study: Path, inference: Path, archive: Path, private: Path):
    protocol = json.loads(PROTOCOL.read_bytes())
    _, cohorts, groups, arms, cache, _ = verified_inputs(study)
    report = json.loads((inference / "report.json").read_bytes())
    if (
        data.sha256(inference / "inference.jsonl") != report["inference_file_sha256"]
        or report["full_direct_followup_confirmation"]
    ):
        raise ValueError("inference sources differ from fixed cached transfer scores")
    for name, p in (
        ("candidate_protocol_sha256", data.PROTOCOL),
        ("development_rankings_sha256", study / "development/rankings.jsonl"),
        ("confirmation_rankings_sha256", study / "confirmation/rankings.jsonl"),
        ("rerank_protocol_sha256", data.PUBLIC / "rerank-protocol.json"),
        ("official_archive_sha256", archive),
    ):
        if data.sha256(p) != protocol["inputs"][name]:
            raise ValueError("follow-up input fingerprint mismatch")
    for row in data.jsonl(inference / "inference.jsonl"):
        values = cache[row["query_id"]]
        for chunk, logit in row["scores"].items():
            values.setdefault(chunk, logit)
    mp = json.loads(MODEL_PROTOCOL.read_bytes())
    source = frozen_source(mp)
    chunks = {h["chunk_id"]: h for h in chunk_manifest(source, data.SNAPSHOT, mp)}
    claims, official = archive_records(archive)
    states = {}
    with private.open("x", encoding="utf8", newline="\n") as stream:
        for q, m in arms.items():
            old = m["baseline"]
            old_ids = {h["chunk_id"] for h in old}
            gold = {d for d, g in source.qrels[q].items() if g > 0}
            annotated = {d: e for d, e in claims[q]["evidence"].items() if e}
            locations = {d: sentence_ranges(source.corpus[d], official[d]) for d in annotated}
            old_rank = rank(old, cache[q])
            best = {}
            for h in old_rank:
                if h["document_id"] in gold:
                    best.setdefault(h["document_id"], h["chunk_id"])
            lexical_prefix = [h for h in m["lexical_refill4"] if h["chunk_id"] not in old_ids]
            candidate = protected_refill(old, cache[q], lexical_prefix)
            new_ids = {h["chunk_id"] for h in candidate} - old_ids
            if len(candidate) != 20 or not new_ids <= {h["chunk_id"] for h in lexical_prefix}:
                raise ValueError("new protected pools escaped cached candidates")
            stream.write(
                json.dumps({"query_id": q, METHOD: candidate}, separators=(",", ":")) + "\n"
            )
            per_method = {}
            for arm, pool in (
                ("baseline", old),
                ("lexical_refill4", m["lexical_refill4"]),
                (METHOD, candidate),
            ):
                ranked = rank(pool, cache[q])
                doc_rank = [h["document_id"] for h in ranked]
                gold5 = set(doc_rank[:5]) & gold
                gold20 = set(doc_rank) & gold
                official_groups = {}
                for stage, hits in (("pool_rationales", pool), ("raw5_rationales", ranked[:5])):
                    ok = set()
                    for d, e in annotated.items():
                        windows = [
                            (chunks[h["chunk_id"]]["char_start"], chunks[h["chunk_id"]]["char_end"])
                            for h in hits
                            if h["document_id"] == d
                        ]
                        if complete_rationale(e, locations[d], windows):
                            ok.add(d)
                    official_groups[stage] = ok
                metric = evaluate_questions({q: source.qrels[q]}, {q: doc_rank}).metrics
                current = {h["chunk_id"] for h in pool}
                per_method[arm] = {
                    **official_groups,
                    "pool_positive_sources": gold20,
                    "raw5_positive_sources": gold5,
                    "pool_recall": len(gold20) / len(gold),
                    "raw5_recall": len(gold5) / len(gold),
                    "document_ndcg10": metric["nDCG@10"],
                    "document_mrr5": metric["MRR@5"],
                    "document_recall5": metric["Recall@5"],
                    "extra_bge_pairs": len(new_ids) if arm == METHOD else 0,
                    "chosen_new_chunks": len(current - old_ids),
                    "discarded_baseline_best_gold_chunks": len(set(best.values()) - current),
                    "gold_count": len(gold),
                    "rationale_count": len(annotated),
                }
            states[q] = per_method
    missing = [
        q for q in cohorts["development"] if not states[q]["baseline"]["pool_positive_sources"]
    ]
    if len(missing) != 13:
        raise ValueError("original complete-miss cohort differs")
    return {
        "protocol_sha256": data.sha256(PROTOCOL),
        "source_sha256": data.sha256(Path(__file__)),
        "inference_sha256": report["inference_file_sha256"],
        "protected_candidates_sha256": data.sha256(private),
        "study": "single exploratory configuration on exposed TRAIN; no new model inference",
        "cohorts": {
            name: summary(ids, states, groups)
            for name, ids in (
                ("development", cohorts["development"]),
                ("confirmation_previously_exposed", cohorts["confirmation"]),
                ("thirteen_previous_misses", missing),
            )
        },
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("study", "inference", "archive", "private", "output"):
        cli.add_argument("--" + name, type=Path, required=True)
    args = cli.parse_args()
    data.save(args.output, run(args.study, args.inference, args.archive, args.private))


if __name__ == "__main__":
    main()
