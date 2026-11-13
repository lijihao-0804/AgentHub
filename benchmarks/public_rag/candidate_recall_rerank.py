"""Measure the transfer of fixed candidate policies through the real BGE cross-encoder."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
from collections import Counter
from pathlib import Path

import numpy as np

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.scoring import evaluate_questions
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.contracts import RerankCandidate

ARMS = ("baseline", "safe_refill4", "lexical_refill4")
PROTOCOL = data.PUBLIC / "rerank-protocol.json"
RESULTS = data.HISTORY.parent
SUPERSET = RESULTS / "budget-diversity-20261022/screen-superset.jsonl"
MODEL_PROTOCOL = RESULTS / "published/reranker-batching-20261114/protocol.json"


def verified_inputs(run: Path):
    protocol = json.loads(PROTOCOL.read_bytes())
    cohorts, groups = data.checked_cohorts(run)
    if data.sha256(data.PROTOCOL) != protocol["candidate_protocol_sha256"]:
        raise ValueError("candidate study has a different protocol")
    for name, digest in protocol["history_sha256"].items():
        if data.sha256(RESULTS / name) != digest:
            raise ValueError(f"historical reranker scores differ: {name}")
    meta = json.loads(SUPERSET.with_suffix(".meta.json").read_bytes())
    identity = meta["retrieval_identity"]
    if (meta["split"], meta["snapshot_id"], meta["arm"], identity["reranker_model"]) != (
        "train",
        json.loads(data.PROTOCOL.read_bytes())["snapshot_id"],
        "superset",
        "BAAI/bge-reranker-v2-m3",
    ):
        raise ValueError("incorrect BGE score provenance")
    candidates = {}
    for phase, digest in protocol["rankings_sha256"].items():
        path = run / phase / "rankings.jsonl"
        if data.sha256(path) != digest:
            raise ValueError(f"fixed {phase} candidates differ")
        rows = list(data.jsonl(path))
        if [r["query_id"] for r in rows] != cohorts[phase]:
            raise ValueError("candidate order differs from registration")
        candidates.update({r["query_id"]: {m: r[m] for m in ARMS} for r in rows})
    old = data.historical_rows(list(candidates))
    superset = {r["query_id"]: r["stages"] for r in data.jsonl(SUPERSET) if r["query_id"] in old}
    cache, drift = {}, {}
    for q, arms in candidates.items():
        historical = old[q]
        full = superset[q]
        for lane in ("dense", "sparse"):
            if historical[lane]["ranking"] != full[lane]["ranking"]:
                raise ValueError("historical lanes differ between score caches")
        union = {
            h["chunk_id"]: h["document_id"]
            for route in ("dense", "sparse")
            for h in full[route]["ranking"]
        }
        full_scores = {h["chunk_id"]: h["score"] for h in full["rerank"]["ranking"]}
        base_scores = {h["chunk_id"]: h["score"] for h in historical["rerank"]["ranking"]}
        if (
            {h["chunk_id"]: h["document_id"] for h in full["rerank"]["ranking"]} != union
            or set(base_scores) != {h["chunk_id"] for h in arms["baseline"]}
            or not all(math.isfinite(v) for v in (*full_scores.values(), *base_scores.values()))
        ):
            raise ValueError("reranked rows do not exhaust the original retrieval pool")
        for hits in arms.values():
            for h in hits:
                if h["chunk_id"] in union and union[h["chunk_id"]] != h["document_id"]:
                    raise ValueError("source IDs conflict between score and candidate caches")
        cache[q] = {**full_scores, **base_scores}
        drift[q] = [abs(score - full_scores[c]) for c, score in base_scores.items()]
    return protocol, cohorts, groups, candidates, cache, drift


def rank(hits: list, scores: dict) -> list:
    if any(h["chunk_id"] not in scores or not math.isfinite(scores[h["chunk_id"]]) for h in hits):
        raise ValueError("missing BGE score for measured candidate")
    return sorted(hits, key=lambda h: (-scores[h["chunk_id"]], -h["score"], h["chunk_id"]))


def direct(model, query, hits, chunks):
    pairs = []
    for h in hits:
        doc, text = chunks[h["chunk_id"]]
        if doc != h["document_id"]:
            raise ValueError("rerank argument text belongs to another source")
        pairs.append(RerankCandidate(h["chunk_id"], text))
    logits = model.rerank(query, pairs)
    if len(logits) != len(hits) or not all(math.isfinite(v) for v in logits):
        raise ValueError("incomplete real BGE inference")
    return {hit["chunk_id"]: value for hit, value in zip(hits, logits, strict=True)}


def drift_summary(diffs: list) -> dict:
    return {"pairs": len(diffs), "equal": diffs.count(0.0), "max_abs": max(diffs, default=0.0)}


def audit_direct(candidates, cache, direct_scores, qrels, ids):
    audit = {}
    for arm in ("baseline", "lexical_refill4"):
        diffs, top5_agreement, gold_agreement = [], 0, 0
        for q in ids:
            scores = direct_scores[q][arm]
            hits = candidates[q][arm]
            final = {**scores, **cache[q]}
            diffs.extend(abs(v - cache[q][c]) for c, v in scores.items() if c in cache[q])
            mixed = rank(hits, final)[:5]
            real = rank(hits, scores)[:5]
            gold = {d for d, grade in qrels[q].items() if grade > 0}
            top5_agreement += {h["chunk_id"] for h in mixed} == {h["chunk_id"] for h in real}
            gold_agreement += ({h["document_id"] for h in mixed} & gold) == (
                {h["document_id"] for h in real} & gold
            )
        stats = drift_summary(diffs)
        audit[arm] = {
            "queries": len(ids),
            "comparable_cached_logits": stats,
            "exact_top5_chunk_sets": top5_agreement,
            "exact_raw5_positive_source_sets": gold_agreement,
            "passed": top5_agreement / len(ids) >= 0.98
            and gold_agreement == len(ids)
            and stats["max_abs"] <= 0.125,
        }
    return audit


def summarize(ids, groups, qrels, candidates, scores, phase):
    labels = {q: qrels[q] for q in ids}
    grouped = sorted({groups[q] for q in ids})
    gid = np.array([grouped.index(groups[q]) for q in ids])
    draws = np.random.default_rng(20261114).integers(len(grouped), size=(10000, len(grouped)))
    denom = np.bincount(gid)[draws].sum(axis=1)
    values, order = {}, {}
    for arm in ARMS:
        order[arm] = {q: rank(candidates[q][arm], scores[q][arm]) for q in ids}
        docs = {q: [h["document_id"] for h in order[arm][q]] for q in ids}
        ev = evaluate_questions(labels, docs)
        first5 = evaluate_questions(labels, {q: d[:5] for q, d in docs.items()})
        if ev.evaluated_count != len(ids) or first5.evaluated_count != len(ids):
            raise ValueError("incomplete BGE scoring denominator")
        values[arm] = {
            "raw5_source_recall": [first5.questions[q].gold_coverage for q in ids],
            **{
                name: [ev.questions[q].metrics[m] for q in ids]
                for name, m in (
                    ("document_ndcg10", "nDCG@10"),
                    ("document_recall5", "Recall@5"),
                    ("document_mrr5", "MRR@5"),
                )
            },
        }
    result = {}
    for arm in ARMS[1:]:
        metrics = {}
        for name, baseline in values["baseline"].items():
            current = values[arm][name]
            delta = np.asarray(current) - baseline
            draws_mean = np.bincount(gid, weights=delta)[draws].sum(axis=1) / denom
            lo, hi = np.quantile(draws_mean, [0.0125, 0.9875]).tolist()
            metrics[name] = {
                "baseline": float(np.mean(baseline)),
                "candidate": float(np.mean(current)),
                "delta": float(np.mean(delta)),
                "paired_ci97_5": [lo, hi],
            }
        counts, examples = Counter(), {}
        for q in ids:
            old, new = order["baseline"][q], order[arm][q]
            new_chunks = {h["chunk_id"] for h in new}
            gold = {d for d, g in labels[q].items() if g > 0}
            old5, new5 = ({h["document_id"] for h in a[:5]} & gold for a in (old, new))
            counts.update(
                raw5_baseline_pairs=len(old5),
                raw5_candidate_pairs=len(new5),
                raw5_retained_pairs=len(old5 & new5),
                raw5_gained_pairs=len(new5 - old5),
                raw5_lost_pairs=len(old5 - new5),
                any_baseline=int(bool(old5)),
                any_candidate=int(bool(new5)),
                any_gained=int(not old5 and bool(new5)),
                any_lost=int(bool(old5) and not new5),
                unchanged_raw5_chunk_set=int(
                    {h["chunk_id"] for h in old[:5]} == {h["chunk_id"] for h in new[:5]}
                ),
                gained_in_pool_and_raw5=len((new5 - old5) - {h["document_id"] for h in old}),
            )
            for doc in sorted(gold):
                before = [(i, h) for i, h in enumerate(old, 1) if h["document_id"] == doc]
                after = [(i, h) for i, h in enumerate(new, 1) if h["document_id"] == doc]
                prev = before[0] if before else None
                best = after[0] if after else None
                dropped_best_chunk = bool(prev and prev[1]["chunk_id"] not in new_chunks)
                counts["best_baseline_positive_chunk_removed"] += int(dropped_best_chunk)
                counts["top5_positive_pairs_lost_with_best_removed"] += int(
                    dropped_best_chunk and doc in old5 - new5
                )
                counts["top5_positive_pairs_lost_with_best_retained"] += int(
                    not dropped_best_chunk and doc in old5 - new5
                )
                categories = {
                    "first_delivered_loss": doc in old5 - new5,
                    "first_delivered_gain": doc in new5 - old5,
                    "first_removed_best_gold_block": dropped_best_chunk,
                }
                for label, yes in categories.items():
                    if yes and label not in examples:
                        examples[label] = {
                            "query_id": q,
                            "positive_document_id": doc,
                            "baseline_best_rank": prev[0] if prev else None,
                            "candidate_best_rank": best[0] if best else None,
                            "baseline_best_score": scores[q]["baseline"][prev[1]["chunk_id"]]
                            if prev
                            else None,
                            "candidate_best_score": scores[q][arm][best[1]["chunk_id"]]
                            if best
                            else None,
                            "original_best_gold_chunk_still_in_pool": bool(
                                prev and prev[1]["chunk_id"] in new_chunks
                            ),
                        }
        checks = {}
        for m in ("raw5_source_recall", "document_ndcg10"):
            metric = metrics[m]
            checks[m] = metric["delta"] >= -0.002 and metric["paired_ci97_5"][0] >= -0.005
        result[arm] = {
            "metrics": metrics,
            "counts": dict(counts),
            "examples": examples,
            "registered_downstream_noninferiority": checks if phase == "confirmation" else None,
            "positive_raw5_delivery_evidence": metrics["raw5_source_recall"]["paired_ci97_5"][0]
            > 0,
        }
    return result


def run(working: Path, output: Path):
    protocol, cohorts, groups, candidates, cache, drift = verified_inputs(working)
    output.mkdir(parents=True, exist_ok=False)
    scores_protocol = json.loads(MODEL_PROTOCOL.read_bytes())["model"]
    model_dir = (
        data.ROOT.parent
        / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots"
        / scores_protocol["revision"]
    )
    for name, digest in scores_protocol["file_sha256"].items():
        if data.sha256(model_dir / name) != digest:
            raise ValueError("local BGE model files differ")
    texts, chunks = data.queries(list(candidates)), data.snapshot_chunks()
    qrels = data.train_qrels(set(candidates))
    ids = list(candidates)
    audit_ids = cohorts["development"][:16] + cohorts["confirmation"][:64]
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    model = BgeReranker(model_name=str(model_dir), device="cuda", batch_size=8, max_length=8192)
    model.warm()
    import torch

    if str(next(model._model.parameters()).dtype) != "torch.float16" or model._device != "cuda":
        raise ValueError("real GPU BGE FP16 inference required")
    extra, audit_scores = {}, {}
    with (output / "inference.jsonl").open("x", encoding="utf-8", newline="\n") as stream:

        def record(q, arm, hits):
            result = direct(model, texts[q], hits, chunks)
            stream.write(json.dumps({"query_id": q, "score_source": arm, "scores": result}) + "\n")
            stream.flush()
            return result

        for q in ids:
            arms = candidates[q]
            if q in audit_ids:
                audit_scores[q] = {
                    a: record(q, a, arms[a]) for a in ("baseline", "lexical_refill4")
                }
            missing = [h for h in arms["lexical_refill4"] if h["chunk_id"] not in cache[q]]
            if q in audit_scores:
                extra[q] = {
                    h["chunk_id"]: audit_scores[q]["lexical_refill4"][h["chunk_id"]]
                    for h in missing
                }
            else:
                extra[q] = record(q, "out_of_union", missing) if missing else {}
        audit = audit_direct(candidates, cache, audit_scores, qrels, audit_ids)
        score_maps = {q: {arm: {**extra[q], **cache[q]} for arm in ARMS} for q in ids}
        full_rescores = {}
        if any(not v["passed"] for v in audit.values()):
            full_rescores["lexical_refill4"] = True
            if audit["baseline"]["exact_raw5_positive_source_sets"] != len(audit_ids):
                full_rescores["baseline"] = True
            for q in cohorts["confirmation"]:
                for arm in full_rescores:
                    source = audit_scores.get(q, {}).get(arm)
                    score_maps[q][arm] = (
                        source
                        if source is not None
                        else record(q, arm + "_complete", candidates[q][arm])
                    )
    report = {
        "protocol_sha256": data.sha256(PROTOCOL),
        "implementation_sha256": data.sha256(Path(__file__)),
        "weights_revision": scores_protocol["revision"],
        "python": platform.python_version(),
        "torch": str(torch.__version__),
        "device": torch.cuda.get_device_name(0),
        "precision": "torch.float16",
        "batch_size": 8,
        "max_length": 8192,
        "audited_full_queries": len(audit_ids),
        "direct_cache_checks": audit,
        "full_direct_followup_confirmation": list(full_rescores),
        "inference_file_sha256": data.sha256(output / "inference.jsonl"),
        "cohorts": {},
    }
    for phase in ("development", "confirmation"):
        selected = cohorts[phase]
        old_direct = len(selected) * 20
        src = Counter()
        for q in selected:
            old_chunks = {h["chunk_id"] for h in candidates[q]["baseline"]}
            for h in candidates[q]["lexical_refill4"]:
                origin = (
                    "original_baseline"
                    if h["chunk_id"] in old_chunks
                    else ("full_union" if h["chunk_id"] in cache[q] else "fresh_gpu_logits")
                )
                src[origin] += 1
        report["cohorts"][phase] = {
            "queries": len(selected),
            "source_components": len({groups[q] for q in selected}),
            "baseline_scores": old_direct,
            "lexical_nominee_score_sources_before_direct_followup": dict(src),
            "original_pool_vs_full_union_common_logits": drift_summary(
                [v for q in selected for v in drift[q]]
            ),
            "results": summarize(selected, groups, qrels, candidates, score_maps, phase),
        }
    data.save(output / "report.json", report)
    print(
        json.dumps(
            {
                "phase": "complete",
                "audited": len(audit_ids),
                "full_direct_followup_confirmation": list(full_rescores),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.study, args.output)


if __name__ == "__main__":
    main()
