"""One frozen, paper-scoped reading-recovery pilot using real BGE/Qdrant adapters.

No SQL, AgentVersion, AgentRun, answer model or production collection is touched.
Both views share exactly the same query, indexed leaves and reranked candidates.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from qdrant_client import QdrantClient

from benchmarks.public_rag.experimental.paragraph_recovery import leaf_view, recover_paragraphs
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.paragraph_qa_live import article_and_spans
from benchmarks.public_rag.scifact_runner import offline_models
from benchmarks.public_rag.source_range_coverage import covered_characters, locate_unique
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.context_selection import select_budgeted_records
from packages.knowledge.contracts import RerankCandidate, VectorRecord, VectorScope
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.retrieval import fuse_reciprocal_rank

ROOT = Path("benchmarks/public_rag/results/rag-root-cause-20261024/paragraph-recovery-1")
DATA = ROOT.parent / "development-proposal-v2.json"
SEED = 2026102411
ARMS = ("leaf", "paragraph")
CONFIG = {
    "chunk_chars": 1200,
    "overlap_chars": 200,
    "dense_k": 20,
    "sparse_k": 20,
    "fusion_k": 20,
    "rrf_k": 60,
    "anchors": 10,
    "record_array_bytes": 8000,
    "parent_max_chars": 1800,
    "rerank_floor": "NONE_SHARED_OFFLINE_POLICY",
}
ADVANCE_GATE = {
    "min_complete_annotation_delta": 0.10,
    "ci95_lower_above": 0,
    "all_source_views_valid": True,
    "record_array_max_bytes": 8000,
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def model_specs():
    cache = Path("../hf_cache").resolve()
    specs = {}
    for name, repo in (("dense", "BAAI/bge-m3"), ("reranker", "BAAI/bge-reranker-v2-m3")):
        folder = cache / ("models--" + repo.replace("/", "--"))
        revision = (folder / "refs/main").read_text().strip()
        if not (folder / "snapshots" / revision).is_dir():
            raise ValueError("frozen local model revision is absent")
        specs[name] = {"repo": repo, "revision": revision}
    return specs


def model_path(spec):
    return str(
        Path("../hf_cache").resolve()
        / ("models--" + spec["repo"].replace("/", "--"))
        / "snapshots"
        / spec["revision"]
    )


def freeze():
    from benchmarks.public_rag.reader_isolation import clean_build

    commit = clean_build()
    data = read(DATA)
    papers = []
    for paper in data["corpus"]:
        article, spans = article_and_spans(paper)
        revision, kb = uuid4(), str(uuid4())
        parsed = ParsedDocument((ParsedBlock(article, {"type": "text_range"}),))
        # Reuse the production immutable chunk builder; its required identities
        # are ingestion work, not an additional integrity/hash-test pass.
        chunks = build_deterministic_chunks(
            revision, parsed, chunk_size_chars=1200, chunk_overlap_chars=200
        )
        papers.append(
            {
                "paper_id": paper["paper_id"],
                "title": paper["title"],
                "revision_id": str(revision),
                "knowledge_base_id": kb,
                "article": article,
                "spans": spans,
                "chunks": [{**asdict(c), "point_id": str(uuid4())} for c in chunks],
            }
        )
    protocol = {
        "state": "FROZEN_DEVELOPMENT_RECOVERY_PILOT",
        "build_commit": commit,
        "dataset": DATA.as_posix(),
        "registered_queries": len(data["cases"]),
        "query_ids": [c["query_id"] for c in data["cases"]],
        "seed": SEED,
        "config": CONFIG,
        "models": model_specs(),
        "workspace_id": str(uuid4()),
        "hypothesis": (
            "A bounded original paragraph can restore a fact split across indexed leaves."
        ),
        "fixed": (
            "Same real embedding/sparse/RRF/reranker and leaf index; same atomic record budget."
        ),
        "candidate": "One declared parent by largest leaf overlap, <=1800 chars, no neighbors.",
        "gold_policy": (
            "Annotations are offline scorer input only, never retrieval or parent selection."
        ),
        "answer_model_requests": 0,
        "measurement": "Exact source-coordinate annotation union, not semantic sufficiency.",
        "advance_gate": ADVANCE_GATE,
        "gate_meaning": (
            "Only qualifies a fixed answer experiment; never promotes based on coverage."
        ),
        "stop": "Failed gate means no answer API experiment and no parameter scan.",
        "runtime_scope": (
            "QdrantLocal memory and production model adapters; not server performance, "
            "tenant integration, published snapshots, PDF parsing or AgentRun E2E."
        ),
        "formal_experiment": False,
    }
    ROOT.mkdir()
    write(ROOT / "protocol.json", protocol)
    write(ROOT / "prepared.json", {"papers": papers})
    print(
        json.dumps(
            {
                "queries": len(data["cases"]),
                "papers": len(papers),
                "indexed_chunks": sum(len(p["chunks"]) for p in papers),
                "model_calls": 0,
            }
        )
    )


def choose_views(paper, anchors, arm):
    if arm == "leaf":
        views = [
            leaf_view(a, paper["article"], paper["paper_id"], paper["revision_id"]) for a in anchors
        ]
    elif arm == "paragraph":
        views = recover_paragraphs(
            anchors,
            paper["article"],
            paper["spans"],
            paper["paper_id"],
            paper["revision_id"],
            max_chars=CONFIG["parent_max_chars"],
        )
    else:
        raise ValueError("unregistered reading policy")
    by_id = {v["source_id"]: v for v in views}
    kept = select_budgeted_records(
        [{"source_id": v["source_id"], "text": v["text"]} for v in views],
        max_bytes=CONFIG["record_array_bytes"],
        max_items=CONFIG["anchors"],
    )
    actual = [by_id[v["source_id"]] for v in kept]
    for view in actual:
        if view["paper_id"] != paper["paper_id"] or view["revision_id"] != paper["revision_id"]:
            raise ValueError("reading scope changed")
        span = view["source_ranges"][0]
        if paper["article"][span["char_start"] : span["char_end"]] != view["text"]:
            raise ValueError("reading view changed its source meaning")
    size = len(json.dumps(list(kept), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {
        "records": actual,
        "wire_bytes": size,
        "before_records": len(views),
        "semantic_sufficiency": "UNKNOWN",
    }


def coverage(case, paper, views):
    visible = [
        (r["source_ranges"][0]["char_start"], r["source_ranges"][0]["char_end"])
        for r in views["records"]
    ]
    values, unaligned = [], 0
    for annotation in case["annotations"]:
        if annotation["answer_type"] == "unanswerable" or not annotation["evidence"]:
            continue
        required = [locate_unique(paper["article"], e) for e in annotation["evidence"]]
        if any(span is None for span in required):
            unaligned += 1
            continue
        values.append(covered_characters(required, visible))
    return {
        "evaluable": bool(values),
        "unaligned_annotations": unaligned,
        "complete": int(any(v["complete"] for v in values)) if values else None,
        "char_fraction": max(v["fraction"] for v in values) if values else None,
    }


def pilot():
    from benchmarks.public_rag.reader_isolation import clean_build

    protocol = read(ROOT / "protocol.json")
    if (
        clean_build() != protocol["build_commit"]
        or protocol["config"] != CONFIG
        or protocol["advance_gate"] != ADVANCE_GATE
    ):
        raise ValueError("pilot source or configuration changed since freeze")
    data, prepared = read(DATA), read(ROOT / "prepared.json")
    if [c["query_id"] for c in data["cases"]] != protocol["query_ids"]:
        raise ValueError("registered query population changed")
    out = ROOT / "pilot-1"
    out.mkdir()
    write(
        out / "started.json",
        {"registered_queries": len(data["cases"]), "answer_model_calls": 0, "state": "STARTED"},
    )
    offline_models()
    dense = BgeM3DenseEmbedder(model_name=model_path(protocol["models"]["dense"]), device="cuda")
    sparse = BgeM3SparseEncoder(model_name=model_path(protocol["models"]["dense"]))
    reranker = BgeReranker(model_name=model_path(protocol["models"]["reranker"]), device="cuda")
    started = time.perf_counter()
    for model in (dense, sparse, reranker):
        model.warm()
    load_ms = (time.perf_counter() - started) * 1000
    client = QdrantClient(":memory:")
    index = QdrantVectorIndex(
        url="http://unused.invalid",
        collection_name="reading_pilot",
        dense_vector_size=1024,
        client=client,
    )
    index.ensure_collection()
    papers = {p["paper_id"]: p for p in prepared["papers"]}
    chunks = {c["chunk_id"]: c for p in papers.values() for c in p["chunks"]}
    for paper in papers.values():
        texts = [c["text"] for c in paper["chunks"]]
        vectors, lexicals = dense.embed_documents(texts), sparse.encode_documents(texts)
        records = [
            VectorRecord(
                c["point_id"],
                c["chunk_id"],
                vector,
                lexical,
                {
                    "workspace_id": protocol["workspace_id"],
                    "knowledge_base_id": paper["knowledge_base_id"],
                    "document_revision_id": paper["revision_id"],
                    "chunk_id": c["chunk_id"],
                },
            )
            for c, vector, lexical in zip(paper["chunks"], vectors, lexicals, strict=True)
        ]
        index.upsert(records)
    results = []
    try:
        with (out / "retrievals.jsonl").open("x", encoding="utf-8") as log:
            for case in data["cases"]:
                paper = papers[case["paper_id"]]
                scope = VectorScope(
                    protocol["workspace_id"], paper["knowledge_base_id"], (paper["revision_id"],)
                )
                start = time.perf_counter()
                dh = index.dense_search(dense.embed_query(case["question"]), scope=scope, limit=20)
                sh = index.sparse_search(
                    sparse.encode_query(case["question"]), scope=scope, limit=20
                )
                fused = fuse_reciprocal_rank(dh, sh, rrf_k=60, candidate_top_k=20)
                scores = reranker.rerank(
                    case["question"],
                    [RerankCandidate(c.chunk_id, chunks[c.chunk_id]["text"]) for c in fused],
                )
                if len(scores) != len(fused) or not all(math.isfinite(s) for s in scores):
                    raise ValueError("invalid reranker scores; no coverage comparison")
                ranking = sorted(
                    zip(fused, scores, strict=True), key=lambda pair: (-pair[1], pair[0].chunk_id)
                )
                anchors = [chunks[c.chunk_id] for c, _ in ranking[: CONFIG["anchors"]]]
                views = {arm: choose_views(paper, anchors, arm) for arm in ARMS}
                row = {
                    "query_id": case["query_id"],
                    "paper_id": case["paper_id"],
                    "dense": [asdict(h) for h in dh],
                    "sparse": [asdict(h) for h in sh],
                    "fusion": [asdict(c) for c in fused],
                    "rerank": [{"chunk_id": c.chunk_id, "score": score} for c, score in ranking],
                    "arms": views,
                    "retrieval_and_view_ms": (time.perf_counter() - start) * 1000,
                }
                log.write(json.dumps(row, ensure_ascii=False) + "\n")
                log.flush()
                results.append(row)
                if len(results) % 10 == 0:
                    print(
                        json.dumps(
                            {"completed_queries": len(results), "planned": len(data["cases"])}
                        ),
                        flush=True,
                    )
    finally:
        index.close()
    # Only now consume public evidence annotations for offline coverage scoring.
    evaluated = []
    for case, row in zip(data["cases"], results, strict=True):
        evaluated.append(
            {
                "query_id": case["query_id"],
                "paper_id": case["paper_id"],
                "arms": {a: coverage(case, papers[case["paper_id"]], row["arms"][a]) for a in ARMS},
            }
        )
    eligible = [r for r in evaluated if all(r["arms"][a]["evaluable"] for a in ARMS)]
    if eligible:
        baseline = {r["query_id"]: float(r["arms"]["leaf"]["complete"]) for r in eligible}
        candidate = {r["query_id"]: float(r["arms"]["paragraph"]["complete"]) for r in eligible}
        raw = grouped_difference(
            baseline, candidate, {r["query_id"]: r["paper_id"] for r in eligible}, seed=SEED
        )
        stats = {
            "delta": raw["estimate_ms"],
            "ci95": [raw["ci95_low_ms"], raw["ci95_high_ms"]],
            "queries": raw["queries"],
            "paper_groups": raw["groups"],
            "bootstrap_samples": raw["bootstrap_samples"],
            "units": "fraction",
        }
        advance = (
            stats["delta"] >= ADVANCE_GATE["min_complete_annotation_delta"]
            and stats["ci95"][0] > ADVANCE_GATE["ci95_lower_above"]
        )
    else:
        stats, advance = None, False
    report = {
        "state": "COMPLETE",
        "registered": len(data["cases"]),
        "completed": len(results),
        "coordinate_evaluable": len(eligible),
        "unknown_or_no_positive_annotation": len(data["cases"]) - len(eligible),
        "complete_counts": {a: sum(r["arms"][a]["complete"] for r in eligible) for a in ARMS},
        "paired_coordinate_difference": stats,
        "metric_units": "Dimensionless coverage fractions; latency is reported separately in ms.",
        "advance_to_answers": advance,
        "decision": "QUALIFIES_ANSWER_EXPERIMENT" if advance else "STOP_NO_ANSWER_EXPERIMENT",
        "model_load_ms": load_ms,
        "new_answer_model_calls": 0,
        "source_validation": "all returned views checked against exact immutable article ranges",
        "max_record_array_bytes": max(
            row["arms"][a]["wire_bytes"] for row in results for a in ARMS
        ),
        "semantic_sufficiency": "UNKNOWN",
        "production_promoted": False,
        "cases": evaluated,
    }
    write(out / "report.json", report)
    write(
        out / "completed.json",
        {
            k: report[k]
            for k in ["state", "registered", "completed", "decision", "new_answer_model_calls"]
        },
    )
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "pilot"))
    args = parser.parse_args()
    freeze() if args.action == "freeze" else pilot()
