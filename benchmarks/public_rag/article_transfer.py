"""Paired article/passage retrieval on disjoint NFCorpus TRAIN questions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from scipy import sparse

from benchmarks.public_rag.candidate_recall_data import sha256
from benchmarks.public_rag.candidate_recall_methods import select_candidates
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, direct, rank
from benchmarks.public_rag.data import _load_corpus, _load_qrels, _load_queries
from benchmarks.public_rag.recall_transfer import PROTECTED, ROOT, compare_sources, selected_ids
from benchmarks.public_rag.scifact_recall_forensics import (
    idf_weights,
    normalized_vectors,
    sparse_scores,
)
from benchmarks.public_rag.scifact_recall_index import MODEL_SHA256, vector_matrix
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument

REPORT = Path(__file__).parent / "results/published/article-recall-20261114/transfer"
PROTOCOL = REPORT / "protocol.json"


def load_inputs():
    declaration = json.loads(PROTOCOL.read_bytes())
    previous = json.loads((REPORT / declaration["dataset_inputs"]).read_bytes())
    directory = ROOT / previous["data_directory"]
    for name, digest in previous["input_sha256"].items():
        if sha256(directory / name) != digest:
            raise ValueError(f"Changed NFCorpus input: {name}")
    with (directory / "qrels/train.tsv").open(encoding="utf-8") as stream:
        eligible = {r["query-id"] for r in csv.DictReader(stream, delimiter="\t")}
    for protocol in (previous, json.loads(PROTECTED.read_bytes())):
        eligible.difference_update(selected_ids(protocol, directory))
    if len(eligible) != declaration["eligible_after_exclusion"]:
        raise ValueError("Independent cohort changed")
    salt = declaration["sample_salt"]
    queries = sorted(eligible, key=lambda q: hashlib.sha256(f"{salt}:{q}".encode()).digest())[
        : declaration["sample_size"]
    ]
    if hashlib.sha256("\n".join(queries).encode()).hexdigest() != declaration["ids_sha256"]:
        raise ValueError("Invalid query list")
    return directory, queries


def original_passages(documents):
    passages = []
    for d in documents:
        revision = uuid5(NAMESPACE_URL, f"agenthub/nfcorpus/{d.document_id}")
        parsed = ParsedDocument((ParsedBlock(d.retrieval_text, {"type": "text_range"}),))
        for c in build_deterministic_chunks(
            revision, parsed, chunk_size_chars=1200, chunk_overlap_chars=200
        ):
            passages.append({"document_id": d.document_id, "chunk_id": c.chunk_id, "text": c.text})
    return passages


def verify_model(path, hashes):
    for name, digest in hashes.items():
        if sha256(path / name) != digest:
            raise ValueError(f"Unrecognized model file: {name}")


def build_index(directory: Path, model: Path):
    root, ids = load_inputs()
    verify_model(model, MODEL_SHA256)
    directory.mkdir(parents=True, exist_ok=False)
    docs = list(_load_corpus(root / "corpus.jsonl").values())
    chunks = original_passages(docs)
    queries = _load_queries(root / "queries.jsonl")
    dense = BgeM3DenseEmbedder(model_name=str(model), device="cuda", batch_size=8)
    encoder = BgeM3SparseEncoder(model_name=str(model))
    width = len(encoder._load_tokenizer())
    n = len(chunks)
    if len(docs) != 3633 or n != 6818:
        raise ValueError("Corpus chunking inventory has changed")
    for name, texts in (
        ("passage", [c["text"] for c in chunks]),
        ("parent", [d.retrieval_text for d in docs]),
    ):
        matrix, lex = vector_matrix(texts, dense, encoder, width)
        np.save(directory / f"{name}.npy", normalized_vectors(matrix))
        sparse.save_npz(directory / f"{name}.npz", lex)
    # Individual calls preserve the query model's normal application entry point.
    q_matrix = np.asarray([dense.embed_query(queries[q]) for q in ids], dtype="float32")
    np.save(directory / "queries.npy", normalized_vectors(q_matrix))
    files = ["passage.npy", "passage.npz", "parent.npy", "parent.npz", "queries.npy"]
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "queries": ids,
                "passage_ids": [c["chunk_id"] for c in chunks],
                "parents": [d.document_id for d in docs],
                "files": {f: sha256(directory / f) for f in files},
            }
        ),
        encoding="utf-8",
    )


def top_lane(ids, docs, scores, *, positive=False):
    indices = np.lexsort((ids, -scores))
    if positive:
        indices = indices[scores[indices] > 0]
    return [
        {"document_id": str(docs[i]), "chunk_id": str(ids[i]), "score": float(scores[i])}
        for i in indices[:30]
    ]


def capture(directory: Path, encoder_model: Path, reranker_model: Path, output: Path):
    from benchmarks.public_rag.article_fusion_candidates import select_local_passages
    from benchmarks.public_rag.article_parent_methods import select_parent_chunks

    root, ids = load_inputs()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest["queries"] != ids or any(
        sha256(directory / f) != digest for f, digest in manifest["files"].items()
    ):
        raise ValueError("Index or sample changed")
    verify_model(encoder_model, MODEL_SHA256)
    verify_model(reranker_model, json.loads(MODEL_PROTOCOL.read_bytes())["model"]["file_sha256"])
    docs = list(_load_corpus(root / "corpus.jsonl").values())
    chunks = original_passages(docs)
    parents = np.array([d.document_id for d in docs])
    children = np.array([c["chunk_id"] for c in chunks])
    if parents.tolist() != manifest["parents"] or children.tolist() != manifest["passage_ids"]:
        raise ValueError("Document identity or chunk order changed")
    sources = np.array([c["document_id"] for c in chunks])
    by_source, positions = defaultdict(list), defaultdict(list)
    for i, c in enumerate(chunks):
        by_source[c["document_id"]].append(c)
        positions[c["document_id"]].append(i)
    lookup = {c["chunk_id"]: (c["document_id"], c["text"]) for c in chunks}
    models = {}
    for name in ("passage", "parent"):
        lex = sparse.load_npz(directory / f"{name}.npz").tocsc()
        models[name] = (np.load(directory / f"{name}.npy"), lex, idf_weights(lex))
    qmatrix = np.load(directory / "queries.npy")
    queries = _load_queries(root / "queries.jsonl")
    encoder = BgeM3SparseEncoder(model_name=str(encoder_model))
    model = BgeReranker(model_name=str(reranker_model), device="cuda")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for i, q in enumerate(ids):
            query = queries[q]
            encoding = encoder.encode_query(query)
            lanes = {}
            dense_scores = {}
            for name, (matrix, lex, idf) in models.items():
                item_ids, doc_ids = (children, sources) if name == "passage" else (parents, parents)
                dense_scores[name] = matrix @ qmatrix[i]
                lanes[name] = (
                    top_lane(item_ids, doc_ids, dense_scores[name]),
                    top_lane(item_ids, doc_ids, sparse_scores(lex, idf, encoding), positive=True),
                )
            baseline = select_candidates("baseline", *lanes["passage"])
            parent_ids = [h["document_id"] for h in select_candidates("baseline", *lanes["parent"])]
            best_chunks = {
                d: chunks[
                    min(positions[d], key=lambda j: (-dense_scores["passage"][j], children[j]))
                ]["chunk_id"]
                for d in parent_ids
            }
            arms = {
                "baseline": baseline,
                "article_local_passage": select_local_passages(query, *lanes["passage"], by_source),
                "full_parent4": select_parent_chunks(baseline, parent_ids, best_chunks),
            }
            union = {h["chunk_id"] for lane in lanes["passage"] for h in lane}
            scores = direct(model, query, baseline, lookup)
            result = {}
            for arm, hits in arms.items():
                if len(hits) != 20 or len({h["chunk_id"] for h in hits}) != 20:
                    raise ValueError("Invalid candidate count")
                extras = [h for h in hits if h["chunk_id"] not in scores]
                scores.update(direct(model, query, extras, lookup))
                result[arm] = {
                    "pool": [h["document_id"] for h in hits],
                    "five": [h["document_id"] for h in rank(hits, scores)[:5]],
                    "outside_original_lanes": sum(h["chunk_id"] not in union for h in hits),
                    "new_sources": len(
                        {h["document_id"] for h in hits} - {h["document_id"] for h in baseline}
                    ),
                }
            print(
                json.dumps({"query_id": q, "direct_pairs": len(scores), "arms": result}),
                file=stream,
            )


def score(trace: Path, output: Path):
    root, ids = load_inputs()
    all_labels = _load_qrels(root / "qrels/train.tsv")
    labels = {q: all_labels[q] for q in ids}
    rows = [json.loads(line) for line in trace.read_text(encoding="utf8").splitlines()]
    if [row["query_id"] for row in rows] != ids:
        raise ValueError("Capture incomplete or reordered")
    report = {
        "queries": len(ids),
        "corpus_documents": 3633,
        "chunks": 6818,
        "capture_sha256": sha256(trace),
        "protocol_sha256": sha256(PROTOCOL),
        "bge_pairs_scored": sum(r["direct_pairs"] for r in rows),
        "arms": {},
    }
    for arm in ("article_local_passage", "full_parent4"):
        results = {}
        for stage, depth in (("pool", 20), ("five", 5)):
            before, after = (
                {r["query_id"]: r["arms"][a][stage] for r in rows} for a in ("baseline", arm)
            )
            hits, changes, recall = compare_sources(labels, before, after, depth)
            results[stage] = {
                "query_hits": hits,
                "source_pairs_changed": changes,
                "macro_recall": recall,
            }
            results[stage]["changes_by_query"] = {}
            for q, grades in labels.items():
                positive = {doc for doc, grade in grades.items() if grade > 0}
                old, new = positive.intersection(before[q]), positive.intersection(after[q])
                if old != new:
                    results[stage]["changes_by_query"][q] = {
                        "retained": sorted(old & new),
                        "gained": sorted(new - old),
                        "lost": sorted(old - new),
                    }
        results["gate_passed"] = (
            results["pool"]["macro_recall"]["delta"] > 0
            and results["five"]["macro_recall"]["delta"] >= 0
            and all(results[s]["query_hits"]["lost"] == 0 for s in ("pool", "five"))
        )
        for field in ("outside_original_lanes", "new_sources"):
            results[field] = sum(row["arms"][arm][field] for row in rows)
        report["arms"][arm] = results
    with output.open("x", encoding="utf8", newline="\n") as stream:
        print(json.dumps(report, indent=2), file=stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="action", required=True)
    build = modes.add_parser("index")
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--model", required=True, type=Path)
    run = modes.add_parser("capture")
    run.add_argument("--index", required=True, type=Path)
    run.add_argument("--model", required=True, type=Path)
    run.add_argument("--reranker", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    metric = modes.add_parser("score")
    metric.add_argument("--trace", required=True, type=Path)
    metric.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    if args.action == "index":
        build_index(args.output, args.model)
    elif args.action == "capture":
        capture(args.index, args.model, args.reranker, args.output)
    else:
        score(args.trace, args.output)


if __name__ == "__main__":
    main()
