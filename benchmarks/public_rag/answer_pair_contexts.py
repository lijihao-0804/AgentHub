"""Materialize baseline B on synthetic, independently authored per-case snapshots."""

import os
from dataclasses import asdict, replace

import numpy as np

from benchmarks.public_rag.answer_pair_freeze import OUT, load
from benchmarks.public_rag.confirmation_cases import ROOT, save
from benchmarks.public_rag.confirmation_pool import chunks
from benchmarks.public_rag.reranker_batch_benchmark import PROTOCOL, checked_file
from benchmarks.public_rag.scifact_recall_forensics import (
    idf_weights,
    normalized_vectors,
    ranking,
    sparse_scores,
    vector_hit,
)
from benchmarks.public_rag.scifact_recall_index import MODEL, MODEL_SHA256, vector_matrix
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.citation_qa import _select_evidence
from packages.knowledge.contracts import RerankCandidate
from packages.knowledge.evidence_records import drop_overlapping_evidence
from packages.knowledge.retrieval import fuse_reciprocal_rank


def prepare():
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    weights = load(PROTOCOL)["model"]
    directory = (
        ROOT.parent / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots" / weights["revision"]
    )
    for folder, pins in ((MODEL, MODEL_SHA256), (directory, weights["file_sha256"])):
        for name in pins:
            checked_file(folder, name, pins)
    dense = BgeM3DenseEmbedder(model_name=str(MODEL), device="cuda")
    sparse = BgeM3SparseEncoder(model_name=str(MODEL))
    ranker = BgeReranker(model_name=str(directory), device="cuda")
    result = {}
    for case in load(OUT / "citation-cases.json")["cases"]:
        docs = case["documents"]
        sources = {v["source_id"]: (v["source_id"], v["source"]) for v in docs}
        if len(sources) != len(docs):
            raise ValueError("Repeated source in snapshot")
        rows = chunks(sources)
        ids = np.array(list(rows))
        matrix, lexical = vector_matrix(
            [r.text for r in rows.values()], dense, sparse, len(sparse._load_tokenizer())
        )
        lex = lexical.tocsc()
        query = case["input"]
        dense_ids = ranking(
            normalized_vectors(matrix) @ np.asarray(dense.embed_query(query), dtype=np.float32), ids
        )
        sparse_ids = ranking(
            sparse_scores(lex, idf_weights(lex), sparse.encode_query(query)), ids, positive=True
        )
        lanes = [[str(ids[i]) for i in order[:30]] for order in (dense_ids, sparse_ids)]
        pool = fuse_reciprocal_rank(
            *(tuple(map(vector_hit, lane)) for lane in lanes), candidate_top_k=20
        )
        scores = ranker.rerank(
            query, [RerankCandidate(p.chunk_id, rows[p.chunk_id].text) for p in pool]
        )
        ranked = [
            replace(rows[p.chunk_id], retrieval_score=p.retrieval_score, rerank_score=score)
            for p, score in zip(pool, scores, strict=True)
        ]
        ranked.sort(key=lambda r: (-r.rerank_score, -r.retrieval_score, r.chunk_id))
        selected = _select_evidence(tuple(drop_overlapping_evidence(ranked)[:6]), max_chars=6000)
        result[case["case_id"]] = {
            "candidate_ids": [p.chunk_id for p in pool],
            "visible": [dict(number=v.id, **asdict(v.evidence)) for v in selected],
        }
    save(OUT / "citation-contexts.json", result)


if __name__ == "__main__":
    prepare()
