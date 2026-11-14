"""Prepare shared exact-search candidate pools using the pinned production adapters."""

import os
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

import numpy as np

from benchmarks.public_rag.confirmation_cases import (
    OUT,
    ROOT,
    SOURCE_SHA,
    TRAIN,
    digest,
    paper_text,
    read,
    save,
)
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
from packages.knowledge.chunking import build_deterministic_chunks, normalize_chunk_text
from packages.knowledge.contracts import RerankCandidate, RetrievedEvidence
from packages.knowledge.evidence_sentence import SentenceEvidenceReranker
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.retrieval import fuse_reciprocal_rank


def blind_inputs():
    frozen = read(OUT / "cohort.json")["cases"]
    raw = read(TRAIN)
    if digest(TRAIN.read_bytes()) != SOURCE_SHA:
        raise ValueError("Source changed")
    sources, questions = {}, []
    for case in frozen:
        p = case["gold_documents"][0]
        doc = raw[p]
        body = paper_text(doc)
        if digest(body.encode()) != case["source_sha256"]:
            raise ValueError("Rendered paper changed")
        sources[p] = (doc["title"], body)
        questions.append({key: case[key] for key in ("id", "query", "schedule")})
    return sources, questions


def chunks(sources):
    rows = {}
    for pid, (title, body) in sorted(sources.items()):
        revision = uuid5(NAMESPACE_URL, pid + "/" + digest(body.encode()))
        block = ParsedBlock(body, {"type": "text_range", "char_start": 0})
        for c in build_deterministic_chunks(
            revision, ParsedDocument((block,)), chunk_size_chars=1200, chunk_overlap_chars=160
        ):
            if (
                normalize_chunk_text(
                    normalize_chunk_text(body)[c.locator["char_start"] : c.locator["char_end"]]
                )
                != c.text
            ):
                raise ValueError("Source locator mismatch")
            rows[c.chunk_id] = RetrievedEvidence(
                pid,
                str(revision),
                c.chunk_id,
                title,
                c.locator,
                c.text,
                0.0,
                None,
                {"document_name": title},
            )
    return rows


def prepare(destination: Path):
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sources, questions = blind_inputs()
    rows = chunks(sources)
    ids = np.array(list(rows))
    weights = read(PROTOCOL)["model"]
    reranker_dir = (
        ROOT.parent / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots" / weights["revision"]
    )
    for folder, checks in ((MODEL, MODEL_SHA256), (reranker_dir, weights["file_sha256"])):
        for name in checks:
            checked_file(folder, name, checks)
    dense = BgeM3DenseEmbedder(model_name=str(MODEL), device="cuda")
    sparse = BgeM3SparseEncoder(model_name=str(MODEL))
    rerank = BgeReranker(model_name=str(reranker_dir), device="cuda")
    matrix, lexical = vector_matrix(
        [r.text for r in rows.values()], dense, sparse, len(sparse._load_tokenizer())
    )
    matrix, lexical = normalized_vectors(matrix), lexical.tocsc()
    idf = idf_weights(lexical)
    rerank.rerank("warmup", [RerankCandidate("warm", "Independent warmup text.")])
    cases = []
    for q in questions:
        start = perf_counter()
        vector = dense.embed_query(q["query"])
        dense_order = ranking(matrix @ np.asarray(vector, dtype=np.float32), ids)
        sparse_order = ranking(
            sparse_scores(lexical, idf, sparse.encode_query(q["query"])), ids, positive=True
        )
        lanes = [[str(ids[i]) for i in order[:30]] for order in (dense_order, sparse_order)]
        pool = fuse_reciprocal_rank(
            *(tuple(map(vector_hit, lane)) for lane in lanes), candidate_top_k=20
        )
        probes = [RerankCandidate(x.chunk_id, rows[x.chunk_id].text) for x in pool]
        whole = rerank.rerank(q["query"], probes)
        whole_ms = 1000 * (perf_counter() - start)
        start = perf_counter()
        sentence = SentenceEvidenceReranker(rerank).inspect_with_whole(q["query"], probes, whole)
        extra_ms = 1000 * (perf_counter() - start)
        candidates = [
            asdict(replace(rows[x.chunk_id], retrieval_score=x.retrieval_score, rerank_score=w))
            for x, w in zip(pool, whole, strict=True)
        ]
        cases.append(
            {
                **q,
                "lanes": lanes,
                "pool": candidates,
                "sentence_scores": [s.score for s in sentence],
                "whole_pipeline_ms": whole_ms,
                "sentence_extra_ms": extra_ms,
            }
        )
    save(destination, {"sources": len(sources), "chunks": len(rows), "cases": cases})


def receipt(pools, path):
    # Commit measured ranks and times. Each text is recoverable from its immutable
    # source revision and chunk identity, without redistributing the paper content.
    data = read(pools)
    for c in data["cases"]:
        c["pool"] = [[r["chunk_id"], r["retrieval_score"], r["rerank_score"]] for r in c["pool"]]
    save(path, data)


def restore(path, destination):
    sources, _ = blind_inputs()
    rows = chunks(sources)
    data = read(path)
    for c in data["cases"]:
        c["pool"] = [
            asdict(replace(rows[chunk], retrieval_score=fused, rerank_score=whole))
            for chunk, fused, whole in c["pool"]
        ]
    save(destination, data)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--receipt-from", type=Path)
    group.add_argument("--restore-from", type=Path)
    args = parser.parse_args()
    if args.receipt_from:
        receipt(args.receipt_from, args.destination)
    elif args.restore_from:
        restore(args.restore_from, args.destination)
    else:
        prepare(args.destination)
