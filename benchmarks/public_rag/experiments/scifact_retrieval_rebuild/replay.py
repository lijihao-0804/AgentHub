"""Re-run four SciFact TRAIN retrieval arms against a locally rebuilt full index."""

import argparse
import json
import os
import subprocess
from pathlib import Path

import numpy as np
from scipy import sparse
from threadpoolctl import threadpool_limits

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.candidate_recall_methods import WordBM25, select_candidates
from benchmarks.public_rag.candidate_recall_rationales import complete_rationale
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, direct, rank
from benchmarks.public_rag.data import query_groups
from benchmarks.public_rag.reranker_batch_benchmark import checked_file
from benchmarks.public_rag.scifact_recall_forensics import (
    archive_records,
    idf_weights,
    normalized_vectors,
    ranking,
    sentence_ranges,
    sparse_scores,
    vector_hit,
    verify_artifacts,
)
from benchmarks.public_rag.scifact_recall_index import MODEL, frozen_source
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.retrieval import fuse_reciprocal_rank

from .summary import PROTOCOL, build_summary, read, text_hash

ARCHIVE = data.ROOT / ".scratch/scifact-rationale-source-20261114/data.tar.gz"


def make_pools(dense, lexical, words, documents, config):
    dense_hits, sparse_hits = (
        tuple(vector_hit(h["chunk_id"]) for h in lane) for lane in (dense, lexical)
    )
    fused = fuse_reciprocal_rank(
        dense_hits, sparse_hits, rrf_k=config["rrf_k"], candidate_top_k=len(dense) + len(lexical)
    )
    ranked = [
        {"chunk_id": h.chunk_id, "document_id": documents[h.chunk_id], "score": h.retrieval_score}
        for h in fused
    ]
    size = config["pool"]
    baseline = ranked[:size]
    refill = select_candidates("lexical_refill4", dense, lexical, words, limit=size)
    old_ids = {h["chunk_id"] for h in baseline}
    additions = [h for h in refill if h["chunk_id"] not in old_ids]
    extended = ranked[: size + len(additions)]
    if len(refill) != size or len(extended) != size + len(additions):
        raise ValueError("candidate depths differ from the paired design")
    return {
        "rrf20": baseline,
        "bm25_refill20": refill,
        "rrf_expand": extended,
        "bm25_append": baseline + additions,
    }


def search_lane(scores, ids, documents, cutoff, *, positive=False):
    order = ranking(scores, ids, positive=positive)[:cutoff]
    return [
        {"chunk_id": str(ids[i]), "document_id": documents[ids[i]], "score": float(scores[i])}
        for i in order
    ]


def coverage(hits, gold, evidence, positions, windows):
    sources = {h["document_id"] for h in hits}
    visible = {
        doc
        for doc, groups in evidence.items()
        if doc in sources
        and complete_rationale(
            groups,
            positions[doc],
            [windows[h["chunk_id"]] for h in hits if h["document_id"] == doc],
        )
    }
    return sorted(sources & gold), sorted(visible)


def observe(arms, query, reranker, chunks, gold, evidence, positions, windows, cutoff):
    whole_query_cache = {}
    views = {}
    baseline_top = []
    for arm, pool in arms.items():
        ids = tuple(h["chunk_id"] for h in pool)
        if ids not in whole_query_cache:
            whole_query_cache[ids] = direct(reranker, query, pool, chunks)
        top = rank(pool, whole_query_cache[ids])[:cutoff]
        if arm == "rrf20":
            baseline_top = [h["chunk_id"] for h in top]
        pool_src, pool_ev = coverage(pool, gold, evidence, positions, windows)
        top_src, top_ev = coverage(top, gold, evidence, positions, windows)
        views[arm] = [pool_src, top_src, pool_ev, top_ev]
    return views, baseline_top, sum(len(ids) for ids in whole_query_cache)


def run(index: Path, output: Path):
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    config = read(PROTOCOL)
    source_spec = read(MODEL_PROTOCOL)
    source = frozen_source(source_spec)
    protocol = data.verified_protocol()
    groups = query_groups(source.qrels)
    qids = data.make_cohorts(set(source.queries), groups, protocol["split_plan"])[config["cohort"]]
    if (
        len(qids) != config["queries"]
        or data.fingerprint(qids) != config["ordered_query_ids_sha256"]
    ):
        raise ValueError("cohort identity differs from the frozen historical questions")
    history = data.historical_rows(qids)
    claims, official = archive_records(ARCHIVE)

    verify_artifacts(index)
    index_info = read(index / "chunks-ready.json")
    manifest = list(data.jsonl(index / "manifest.jsonl"))
    ids = np.array([r["chunk_id"] for r in manifest])
    documents = {r["chunk_id"]: r["document_id"] for r in manifest}
    chunks = {r["chunk_id"]: (r["document_id"], r["text"]) for r in manifest}
    windows = {r["chunk_id"]: (r["char_start"], r["char_end"]) for r in manifest}
    corpus_vectors = normalized_vectors(np.load(index / "dense.npy"))
    dense_queries = normalized_vectors(np.load(index / "train-query-dense.npy"))
    query_ids = read(index / "train-query-ids.json")
    vectors = dict(zip(query_ids, dense_queries, strict=True))
    token_counts = sparse.load_npz(index / "sparse.npz")
    if (
        index_info["snapshot_hash"] != protocol["snapshot_hash"]
        or len(manifest) != len(documents)
        or len(documents) != source_spec["corpus_chunks"]
        or set(documents.values()) != set(source.corpus)
        or query_ids != list(source.queries)
        or corpus_vectors.shape != (len(ids), dense_queries.shape[1])
        or token_counts.shape[0] != len(ids)
    ):
        raise ValueError("inconsistent snapshot, document, chunk or vector inventory")
    idf = idf_weights(token_counts)
    words = WordBM25(chunks)
    sparse_encoder = BgeM3SparseEncoder(model_name=str(MODEL))
    model = source_spec["model"]
    reranker_path = (
        data.ROOT.parent / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots" / model["revision"]
    )
    for name in model["file_sha256"]:
        checked_file(reranker_path, name, model["file_sha256"])
    scorer = BgeReranker(
        model_name=str(reranker_path),
        device=model["device"],
        batch_size=model["batch_size"],
        max_length=model["max_length"],
    )
    scorer.warm()
    output.mkdir(parents=True, exist_ok=False)
    pairs_path = output / "questions.jsonl"
    with (
        pairs_path.open("x", encoding="utf-8", newline="\n") as dest,
        threadpool_limits(limits=1, user_api="blas"),
    ):
        for q in qids:
            query = source.queries[q]
            gold = {doc for doc, grade in source.qrels[q].items() if grade > 0}
            claim = claims[q]
            evidence = {doc: groups for doc, groups in claim["evidence"].items() if groups}
            if (
                gold != {str(doc) for doc in claim["cited_doc_ids"]}
                or query != claim["claim"]
                or not evidence.keys() <= gold
            ):
                raise ValueError(f"source or evidence labels disagree for query {q}")
            positions = {d: sentence_ranges(source.corpus[d], official[d]) for d in evidence}
            dense = search_lane(corpus_vectors @ vectors[q], ids, documents, config["dense_k"])
            sparse_lane = search_lane(
                sparse_scores(token_counts, idf, sparse_encoder.encode_query(query)),
                ids,
                documents,
                config["sparse_k"],
                positive=True,
            )
            arms = make_pools(dense, sparse_lane, words.query(query), documents, config)
            views, top, inferences = observe(
                arms, query, scorer, chunks, gold, evidence, positions, windows, config["top_k"]
            )
            current_order = {
                "dense30": [h["chunk_id"] for h in dense],
                "sparse30": [h["chunk_id"] for h in sparse_lane],
                "rrf20": [h["chunk_id"] for h in arms["rrf20"]],
                "rrf_top5": top,
            }
            stages = {
                "dense30": "dense",
                "sparse30": "sparse",
                "rrf20": "selection",
                "rrf_top5": "rerank",
            }
            agree = {}
            for name, stage in stages.items():
                actual = current_order[name]
                past = [h["chunk_id"] for h in history[q][stage]["ranking"][: len(actual)]]
                agree[name] = {"order": actual == past, "members": set(actual) == set(past)}
            row = {
                "query": q,
                "gold": sorted(gold),
                "annotated": sorted(evidence),
                "additional_chunks": len(arms["bm25_append"]) - config["pool"],
                "fresh_scored_pairs": inferences,
                "historical_chunk_agreement": agree,
                **views,
            }
            dest.write(json.dumps(row, separators=(",", ":")) + "\n")
    current_hashes = {**index_info["sha256"], **read(index / "complete.json")["sha256"]}
    previous = read(data.PUBLIC / "forensics-summary.json")
    prior_hashes = {
        **previous["model"]["sha256"],
        **previous["article_and_query_vectors"]["sha256"],
    }
    source_files = (
        "packages/knowledge/adapters/embeddings.py",
        "packages/knowledge/adapters/sparse.py",
        "packages/knowledge/adapters/reranker.py",
        "packages/knowledge/retrieval.py",
        "packages/knowledge/chunking.py",
        "benchmarks/public_rag/scifact_recall_index.py",
        "benchmarks/public_rag/candidate_recall_methods.py",
        "benchmarks/public_rag/experiments/scifact_retrieval_rebuild/replay.py",
        "benchmarks/public_rag/experiments/scifact_retrieval_rebuild/summary.py",
    )
    data.save(
        output / "provenance.json",
        {
            "protocol_source_commit": config["source_commit"],
            "git_head_at_measurement": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=data.ROOT, text=True
            ).strip(),
            "source_files_sha256": {n: text_hash(data.ROOT / n) for n in source_files},
            "snapshot_sha256": protocol["snapshot_hash"],
            "source_documents": len(source.corpus),
            "source_chunks": len(manifest),
            "encoded_train_queries": len(vectors),
            "full_stage_input_sha256": source_spec["input_sha256"],
            "official_annotation_archive_sha256": data.sha256(ARCHIVE),
            "index_versions": index_info["versions"],
            "embedding_model_revision": index_info["model_revision"],
            "reranker_model_revision": model["revision"],
            "reranker_config": {
                k: model[k] for k in ("batch_size", "max_length", "device", "dtype")
            },
            "index_files_sha256": current_hashes,
            "index_files_equal_to_archived": {
                n: h == prior_hashes[n] for n, h in current_hashes.items()
            },
            "protocol_sha256": text_hash(PROTOCOL),
            "questions_sha256": text_hash(pairs_path),
        },
    )
    data.save(output / "summary.json", build_summary(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.index, args.output)
