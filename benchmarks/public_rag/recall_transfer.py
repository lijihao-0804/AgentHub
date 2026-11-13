"""Collect a label-blind NFCorpus TRAIN retrieval trace for a frozen transfer check."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from qdrant_client import QdrantClient

from benchmarks.public_rag.candidate_recall_data import sha256
from benchmarks.public_rag.candidate_recall_methods import WordBM25, select_candidates
from benchmarks.public_rag.candidate_recall_protected import protected_refill
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, direct, rank
from benchmarks.public_rag.data import _load_corpus, _load_qrels, _load_queries
from benchmarks.public_rag.scifact_recall_index import MODEL_SHA256
from benchmarks.public_rag.scoring import evaluate_questions
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.contracts import VectorRecord, VectorScope
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.point_ids import deterministic_point_id

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = (
    Path(__file__).parent / "results/published/candidate-recall-20261114/nfcorpus-protocol.json"
)
PROTECTED = PROTOCOL.with_name("nfcorpus-protected-protocol.json")


def selected_ids(protocol: dict, root: Path) -> list[str]:
    with (root / "qrels/train.tsv").open(encoding="utf-8", newline="") as stream:
        eligible = {row["query-id"] for row in csv.DictReader(stream, delimiter="\t")}
    if "exclude_cohort" in protocol:
        prior = json.loads(PROTOCOL.read_bytes())
        eligible.difference_update(selected_ids(prior, root))
        if len(eligible) != protocol["eligible_after_exclusion"]:
            raise ValueError("Independent cohort population changed")
    salt = protocol["sample_salt"]
    selected = sorted(eligible, key=lambda q: hashlib.sha256(f"{salt}:{q}".encode()).digest())[
        : protocol["sample_size"]
    ]
    if hashlib.sha256("\n".join(selected).encode()).hexdigest() != protocol["ids_sha256"]:
        raise ValueError("Independent query registration changed")
    return selected


def capture(
    output: Path, qdrant_url: str, device: str, model: Path, reranker_dir: Path | None = None
) -> None:
    if urlsplit(qdrant_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Use a local test Qdrant instance")
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    root = ROOT / protocol["data_directory"]
    for name, digest in protocol["input_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Input hash mismatch: {name}")
    if model.name != protocol["retrieval"]["dense_revision"]:
        raise ValueError("Select the registered BGE snapshot")
    output.mkdir(parents=True, exist_ok=False)
    corpus, queries = _load_corpus(root / "corpus.jsonl"), _load_queries(root / "queries.jsonl")
    cohort = json.loads(PROTECTED.read_bytes()) if reranker_dir is not None else protocol
    measured = selected_ids(cohort, root)
    if reranker_dir is not None and reranker_dir.name != cohort["revision"]:
        raise ValueError("Select the registered reranker snapshot")
    for filename, digest in MODEL_SHA256.items():
        if sha256(model / filename) != digest:
            raise ValueError(f"Embedding model file mismatch: {filename}")
    if reranker_dir is not None:
        expected = json.loads(MODEL_PROTOCOL.read_bytes())["model"]["file_sha256"]
        for filename, digest in expected.items():
            if sha256(reranker_dir / filename) != digest:
                raise ValueError(f"Reranker model file mismatch: {filename}")
    chunks = []
    revisions = []
    for doc in corpus.values():
        revision = uuid5(NAMESPACE_URL, f"agenthub/nfcorpus/{doc.document_id}")
        revisions.append(str(revision))
        parsed = ParsedDocument((ParsedBlock(doc.retrieval_text, {"type": "text_range"}),))
        chunks.extend(
            (doc.document_id, str(revision), chunk)
            for chunk in build_deterministic_chunks(
                revision, parsed, chunk_size_chars=1200, chunk_overlap_chars=200
            )
        )
    dense = BgeM3DenseEmbedder(model_name=str(model), device=device)
    passages = {chunk.chunk_id: (doc, chunk.text) for doc, _, chunk in chunks}
    words = WordBM25(passages)
    sparse = BgeM3SparseEncoder(model_name=str(model))
    collection = f"nfcorpus_recall_{uuid4().hex}"
    client = QdrantClient(url=qdrant_url, timeout=45)
    index = QdrantVectorIndex(
        url=qdrant_url, collection_name=collection, dense_vector_size=1024, client=client
    )
    scope = VectorScope(str(uuid4()), str(uuid4()), tuple(revisions))
    try:
        if client.info().version != protocol["retrieval"]["qdrant_version"]:
            raise ValueError("Use the registered Qdrant version")
        index.ensure_collection()
        for offset in range(0, len(chunks), 64):
            batch = chunks[offset : offset + 64]
            texts = [chunk.text for _, _, chunk in batch]
            records = tuple(
                VectorRecord(
                    point_id=deterministic_point_id(chunk.chunk_id),
                    chunk_id=chunk.chunk_id,
                    dense=dv,
                    sparse=sv,
                    payload={
                        "workspace_id": scope.workspace_id,
                        "knowledge_base_id": scope.knowledge_base_id,
                        "document_revision_id": rev,
                        "document_id": doc,
                        "chunk_id": chunk.chunk_id,
                    },
                )
                for (doc, rev, chunk), dv, sv in zip(
                    batch, dense.embed_documents(texts), sparse.encode_documents(texts), strict=True
                )
            )
            index.upsert(records)
        if client.count(collection, exact=True).count != len(chunks):
            raise RuntimeError("Index inventory does not match the full corpus")
        reranker = (
            BgeReranker(model_name=str(reranker_dir), device=device) if reranker_dir else None
        )
        with (output / "retrieval.jsonl").open("x", encoding="utf-8") as stream:
            for q in measured:
                query = queries[q]
                stages = {}
                for name, matches in (
                    ("dense", index.dense_search(dense.embed_query(query), scope=scope, limit=30)),
                    (
                        "sparse",
                        index.sparse_search(sparse.encode_query(query), scope=scope, limit=30),
                    ),
                ):
                    stages[name] = {
                        "ranking": [
                            {
                                "chunk_id": h.payload["chunk_id"],
                                "document_id": h.payload["document_id"],
                                "score": h.score,
                            }
                            for h in matches
                        ]
                    }
                stages["lexical"] = {"ranking": words.query(query)}
                if reranker is not None:
                    baseline = select_candidates(
                        "baseline", stages["dense"]["ranking"], stages["sparse"]["ranking"]
                    )
                    scores = direct(reranker, query, baseline, passages)
                    refined = protected_refill(baseline, scores, stages["lexical"]["ranking"])
                    extras = [h for h in refined if h["chunk_id"] not in scores]
                    scores.update(direct(reranker, query, extras, passages))
                    stages["protected"] = {"ranking": refined, "extra_scored": len(extras)}
                    stages["baseline_raw5"] = {"ranking": rank(baseline, scores)[:5]}
                    stages["protected_raw5"] = {"ranking": rank(refined, scores)[:5]}
                stream.write(json.dumps({"query_id": q, "stages": stages}) + "\n")
    finally:
        try:
            if client.collection_exists(collection):
                client.delete_collection(collection)
        finally:
            index.close()
    (output / "capture.json").write_text(
        json.dumps(
            {
                "corpus_documents": len(corpus),
                "chunks": len(chunks),
                "queries": len(measured),
                "ids_sha256": cohort["ids_sha256"],
                "device": device,
                "trace_sha256": hashlib.sha256(
                    (output / "retrieval.jsonl").read_bytes()
                ).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def compare_sources(labels, baseline, candidate, depth):
    """Measure source-pair churn before document deduplication can extend the cutoff."""
    hits = {"retained": 0, "gained": 0, "lost": 0, "missed_both": 0}
    changes = {"new": 0, "lost": 0}
    for query, grades in labels.items():
        positive = {doc for doc, grade in grades.items() if grade > 0}
        if not positive:
            raise ValueError("Unjudged query in registered transfer sample")
        old = positive.intersection(baseline[query][:depth])
        new = positive.intersection(candidate[query][:depth])
        if old:
            hits["retained" if new else "lost"] += 1
        else:
            hits["gained" if new else "missed_both"] += 1
        changes["new"] += len(new - old)
        changes["lost"] += len(old - new)
    metric = f"Recall@{depth}"
    scores = [
        evaluate_questions(labels, {q: ranking[:depth] for q, ranking in lane.items()}).metrics[
            metric
        ]
        for lane in (baseline, candidate)
    ]
    return (
        hits,
        changes,
        {"baseline": scores[0], "candidate": scores[1], "delta": scores[1] - scores[0]},
    )


def score_transfer(capture_dir: Path, method: str, output: Path) -> None:
    """Score a preselected method once using the separate corpus."""
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    root = ROOT / protocol["data_directory"]
    meta = json.loads((capture_dir / "capture.json").read_text(encoding="utf-8"))
    trace = capture_dir / "retrieval.jsonl"
    if hashlib.sha256(trace.read_bytes()).hexdigest() != meta["trace_sha256"]:
        raise ValueError("Unrecognized transfer trace")
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    is_protected = method == "protected_refill"
    cohort = json.loads(PROTECTED.read_bytes()) if is_protected else protocol
    if [r["query_id"] for r in rows] != selected_ids(cohort, root):
        raise ValueError("Scored queries differ from the independent registration")
    if (
        hashlib.sha256((root / "qrels/train.tsv").read_bytes()).hexdigest()
        != protocol["input_sha256"]["qrels/train.tsv"]
    ):
        raise ValueError("Unrecognized transfer judgments")

    baseline, candidate, labels = {}, {}, {}
    train = _load_qrels(root / "qrels/train.tsv")
    for row in rows:
        query = row["query_id"]
        stages = row["stages"]
        dense = stages["dense"]["ranking"]
        sparse = stages["sparse"]["ranking"]
        labels[query] = train[query]
        baseline[query] = [h["document_id"] for h in select_candidates("baseline", dense, sparse)]
        proposed = (
            stages["protected"]["ranking"]
            if is_protected
            else select_candidates(method, dense, sparse, stages["lexical"]["ranking"], limit=20)
        )
        candidate[query] = [h["document_id"] for h in proposed]
    hits, changed_pairs, scores = compare_sources(labels, baseline, candidate, 20)
    report = {
        "corpus": "NFCorpus official TRAIN (128 ordered IDs)",
        "method": method,
        "capture": meta,
        "query_hits": hits,
        "changed_positive_pairs": changed_pairs,
        "macro_document_recall_at_20": scores,
        "guardrail_passed": hits["lost"] == 0 and scores["delta"] >= 0,
    }
    if is_protected:
        report["rerank_pairs"] = sum(
            len(baseline[r["query_id"]]) + r["stages"]["protected"]["extra_scored"] for r in rows
        )

        def ranked_sources(arm):
            return {
                r["query_id"]: [h["document_id"] for h in r["stages"][arm]["ranking"]] for r in rows
            }

        counts, pairs, raw5 = compare_sources(
            labels, ranked_sources("baseline_raw5"), ranked_sources("protected_raw5"), 5
        )
        report["post_bge_first_five"] = {
            **raw5,
            "changed_positive_pairs": pairs,
            "query_hits": counts,
        }
        report["guardrail_passed"] = (
            changed_pairs["lost"] == 0
            and scores["delta"] > 0
            and raw5["delta"] >= 0
            and counts["lost"] == 0
        )
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True, dest="mode")
    collect = commands.add_parser("capture")
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--qdrant-url", required=True)
    collect.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    collect.add_argument("--model-dir", type=Path, required=True)
    collect.add_argument(
        "--reranker-dir",
        type=Path,
        help="Use the separate protected-refill cohort and score both arms.",
    )
    score = commands.add_parser("score")
    score.add_argument("--capture-dir", type=Path, required=True)
    score.add_argument("--method", required=True)
    score.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "capture":
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        capture(
            args.output, args.qdrant_url, args.device, args.model_dir.resolve(), args.reranker_dir
        )
    else:
        score_transfer(args.capture_dir, args.method, args.output)


if __name__ == "__main__":
    main()
