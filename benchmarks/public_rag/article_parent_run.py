"""Exhaustive parent retrieval on the frozen SciFact TRAIN snapshot, then chunk-level BGE."""

import argparse
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import sparse

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.article_parent_methods import select_parent_chunks
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, direct, verified_inputs
from benchmarks.public_rag.scifact_recall_forensics import (
    fuse,
    idf_weights,
    normalized_vectors,
    ranking,
    sparse_scores,
    verify_artifacts,
)
from benchmarks.public_rag.scifact_recall_index import MODEL
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder

SCRATCH = data.ROOT / ".scratch"
CACHE = SCRATCH / "scifact-recall-forensics-20261114"
WORK = SCRATCH / "article-parent-20261114"
STUDY = SCRATCH / "candidate-recall-scifact-20261114"
PUBLIC = data.PUBLIC.parent / "article-recall-20261114/parent"
SIX = ("609", "620", "817", "818", "828", "1387")
METHODS = ("title_parent4", "full_parent4")


def best_dense_children(doc_ids, by_doc, chunks, vectors, query_vector):
    """Resolve article winners to original blocks using dense cosine and logical-ID ties."""
    selected = {}
    for doc in doc_ids:
        indices = by_doc[doc]
        scores = vectors[indices] @ query_vector
        j = min(range(len(indices)), key=lambda k: (-scores[k], chunks[indices[k]]["chunk_id"]))
        selected[doc] = chunks[indices[j]]["chunk_id"]
    return selected


def generate(work: Path):
    verify_artifacts(CACHE)
    title_meta = json.loads((work / "title-index.json").read_bytes())
    for file, digest in title_meta["sha256"].items():
        if data.sha256(work / file) != digest:
            raise ValueError("title index digest mismatch")
    _, _, _, arms, _, _ = verified_inputs(STUDY)
    ids = json.loads((CACHE / "document-ids.json").read_bytes())
    if title_meta["ordered_ids_sha256"] != hashlib.sha256(json.dumps(ids).encode()).hexdigest():
        raise ValueError("title inventory differs from full parents")
    queries = data.queries(list(arms))
    query_ids = json.loads((CACHE / "train-query-ids.json").read_bytes())
    qvec = normalized_vectors(np.load(CACHE / "train-query-dense.npy"))
    chunks = list(data.jsonl(CACHE / "manifest.jsonl"))
    by_doc = defaultdict(list)
    for i, h in enumerate(chunks):
        by_doc[h["document_id"]].append(i)
    cmat = normalized_vectors(np.load(CACHE / "dense.npy"))
    id_array = np.asarray(ids)
    indexes = {}
    for method, folder, prefix in zip(METHODS, (work, CACHE), ("title-", "parent-"), strict=True):
        matrix = sparse.load_npz(folder / (prefix + "sparse.npz")).tocsc()
        indexes[method] = (
            normalized_vectors(np.load(folder / (prefix + "dense.npy"))),
            matrix,
            idf_weights(matrix),
        )
    encoder = BgeM3SparseEncoder(model_name=str(MODEL))
    with (work / "candidates.jsonl").open("x", encoding="utf8") as stream:
        for q, views in arms.items():
            base = views["baseline"]
            qv = qvec[query_ids.index(q)]
            lex = encoder.encode_query(queries[q])
            result = {"query_id": q, "pools": {"baseline": base}, "parent_rankings": {}}
            for method, (mat, smat, idf) in indexes.items():
                dense = ranking(mat @ qv, id_array)
                lexical = ranking(sparse_scores(smat, idf, lex), id_array, positive=True)
                branches = [[ids[i] for i in lane] for lane in (dense, lexical)]
                parent = fuse(*branches)
                best = best_dense_children(parent[:20], by_doc, chunks, cmat, qv)
                result["pools"][method] = select_parent_chunks(base, parent[:20], best)
                result["parent_rankings"][method] = {
                    "dense": branches[0] if q in SIX else branches[0][:30],
                    "sparse": branches[1] if q in SIX else branches[1][:30],
                    "fusion": parent,
                }
            stream.write(json.dumps(result, separators=(",", ":")) + "\n")


def score(work: Path):
    _, _, _, _, cache, _ = verified_inputs(STUDY)
    model_meta = json.loads(MODEL_PROTOCOL.read_bytes())["model"]
    model_dir = (
        data.ROOT.parent
        / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots"
        / model_meta["revision"]
    )
    for file, digest in model_meta["file_sha256"].items():
        if data.sha256(model_dir / file) != digest:
            raise ValueError("reranker fingerprint differs")
    rows = list(data.jsonl(work / "candidates.jsonl"))
    # Dedicated six-case, post-selection diagnostic: one best original chunk
    # per standalone parent RRF winner. Use the identical dense tie rule.
    inventory = list(data.jsonl(CACHE / "manifest.jsonl"))
    by_doc = defaultdict(list)
    for i, h in enumerate(inventory):
        by_doc[h["document_id"]].append(i)
    chunk_vectors = normalized_vectors(np.load(CACHE / "dense.npy"))
    query_vectors = normalized_vectors(np.load(CACHE / "train-query-dense.npy"))
    query_ids = json.loads((CACHE / "train-query-ids.json").read_bytes())
    standalone = {}
    for row in rows:
        q = row["query_id"]
        if q not in SIX:
            continue
        standalone[q] = {}
        qv = query_vectors[query_ids.index(q)]
        for m, lanes in row["parent_rankings"].items():
            docs = lanes["fusion"][:20]
            best = best_dense_children(docs, by_doc, inventory, chunk_vectors, qv)
            hits = [
                {"document_id": d, "chunk_id": best[d], "score": 1 / (i + 1)}
                for i, d in enumerate(docs)
            ]
            standalone[q][m] = hits
    data.save(work / "standalone.json", standalone)
    queries = data.queries([r["query_id"] for r in rows])
    chunks = {
        r["chunk_id"]: (r["document_id"], r["text"]) for r in data.jsonl(CACHE / "manifest.jsonl")
    }
    model = BgeReranker(model_name=str(model_dir), device="cuda", batch_size=8, max_length=8192)
    start = time.perf_counter()
    total = 0
    with (work / "scores.jsonl").open("x", encoding="utf8") as stream:
        for row in rows:
            q = row["query_id"]
            missing = {
                h["chunk_id"]: h
                for hits in (*row["pools"].values(), *standalone.get(q, {}).values())
                for h in hits
                if h["chunk_id"] not in cache[q]
            }
            new = direct(model, queries[q], list(missing.values()), chunks) if missing else {}
            total += len(new)
            stream.write(json.dumps({"query_id": q, "scores": new}) + "\n")
    data.save(
        work / "inference.json",
        {
            "new_query_chunk_pairs": total,
            "seconds": time.perf_counter() - start,
            "candidate_sha256": data.sha256(work / "candidates.jsonl"),
            "standalone_sha256": data.sha256(work / "standalone.json"),
            "score_sha256": data.sha256(work / "scores.jsonl"),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["generate", "score"])
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    {"generate": generate, "score": score}[args.stage](args.work)
