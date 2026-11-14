"""Compare historical lanes with exact float32 chunk retrieval for the 681 replay IDs."""

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import sparse
from threadpoolctl import threadpool_limits

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.article_fusion_study import STUDY, VECTOR
from benchmarks.public_rag.scifact_recall_forensics import (
    idf_weights,
    normalized_vectors,
    ranking,
    sparse_scores,
    verify_artifacts,
)
from benchmarks.public_rag.scifact_recall_index import MODEL
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder


def main(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    verify_artifacts(VECTOR)
    cohorts, _ = data.checked_cohorts(STUDY)
    ids = cohorts["development"] + cohorts["confirmation"]
    history = data.historical_rows(ids)
    texts = data.queries(ids)
    chunk_ids = np.array([h["chunk_id"] for h in data.jsonl(VECTOR / "manifest.jsonl")])
    lookup = {c: i for i, c in enumerate(chunk_ids)}
    chunk_vectors = normalized_vectors(np.load(VECTOR / "dense.npy"))
    queries = normalized_vectors(np.load(VECTOR / "train-query-dense.npy"))
    qids = json.loads((VECTOR / "train-query-ids.json").read_bytes())
    lexical = sparse.load_npz(VECTOR / "sparse.npz")
    idf = idf_weights(lexical)
    encoder = BgeM3SparseEncoder(model_name=str(MODEL))
    indices = np.array([qids.index(q) for q in ids])
    with threadpool_limits(limits=2):
        dense = queries[indices] @ chunk_vectors.T
    audit = {
        k: {
            "exact_order": 0,
            "same_set": 0,
            "overlap_slots": 0,
            "max_abs_score_diff": 0.0,
            "mismatched_ids": [],
        }
        for k in ("dense", "sparse")
    }
    for i, q in enumerate(ids):
        for key, scores in (
            ("dense", dense[i]),
            ("sparse", sparse_scores(lexical, idf, encoder.encode_query(texts[q]))),
        ):
            predicted = chunk_ids[
                ranking(scores, chunk_ids, positive=key == "sparse")[:30]
            ].tolist()
            target = history[q][key]["ranking"]
            original = [h["chunk_id"] for h in target]
            a = audit[key]
            a["exact_order"] += original == predicted
            a["same_set"] += set(original) == set(predicted)
            a["overlap_slots"] += len(set(original) & set(predicted))
            a["max_abs_score_diff"] = max(
                a["max_abs_score_diff"],
                *(abs(float(scores[lookup[h["chunk_id"]]]) - h["score"]) for h in target),
            )
            if predicted != original:
                a["mismatched_ids"].append(q)
    data.save(
        output / "index-parity.json",
        {
            "queries": len(ids),
            "slots_per_branch": len(ids) * 30,
            "route_source_for_all_arms": "recorded historical lists including actual ties",
            "reconstruction": audit,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
