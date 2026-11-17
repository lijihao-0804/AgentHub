"""Paired offline benchmark of full indexing and exact reuse on tracked document revisions.

Run with a migrated, dedicated local PostgreSQL database (AGENTHUB_TEST_DATABASE_URL),
two local Hugging Face model snapshot directories and an output path. See --help.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

from apps.api.knowledge_dependencies import (
    get_agent_run_workspace_context,
    get_ingestion_queue,
    get_retrieval_components,
    get_workspace_context,
)
from apps.api.routes.knowledge import router
from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from benchmarks.retrieval.runner import PROJECT_ROOT, _async_database_url, _context
from packages.control_plane.models import Organization, User, Workspace
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.contracts import RerankCandidate
from packages.knowledge.models import DocumentChunk, DocumentRevision, IngestionJob

COMMITS = {
    "old": "3a9490343b1b5e7828316e3c1db7411cf6291451",
    "new": "8510ccdf420990c0884cc455f0af89068c4ca51f",
}
DOMAIN = "docs/learning/03-domain-model-lifecycle.md"
DURABILITY = "docs/learning/05-durability-events.md"
# Evidence strings are independently selected from the Git sources before retrieving.
PROBES = [
    (
        "retained_content",
        DOMAIN,
        "为什么只保留 K1 的 hash 无法再次装载历史实验的证据？",
        {"old": "不能从摘要反推出原文", "new": "不能从摘要反推出原文"},
    ),
    (
        "sse_replay",
        DURABILITY,
        "实时队列有容量限制时，慢消费者怎么补回错过的事件？",
        {"old": "慢消费者可能丢帧", "new": "慢消费者会退出 live 队列转持久补播"},
    ),
    (
        "run_lease",
        DURABILITY,
        "过期的数据库 Run lease 会怎样处理，是否允许夺取并重放图？",
        {"new": "过期执行权进入 NEEDS_ATTENTION"},
    ),
    (
        "wiki_drift",
        DOMAIN,
        "Wiki publication 丢失或漂移，运行恢复时如何处理？",
        {"new": "WIKI_PUBLICATION_UNAVAILABLE"},
    ),
]
TOP_K = 5


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=PROJECT_ROOT)


class CountDense(BgeM3DenseEmbedder):
    """Count actual document texts while keeping query inference out of intake metrics."""

    documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return super().embed_documents(texts)

    def embed_query(self, text):
        return super().embed_documents((text,))[0]


class CountSparse(BgeM3SparseEncoder):
    documents = 0

    def encode_documents(self, texts):
        self.documents += len(texts)
        return super().encode_documents(texts)


class InlineQueue:
    def __init__(self):
        self.jobs = deque()

    async def enqueue(self, job_id):
        self.jobs.append(job_id)


async def arm_context(factory):
    async with factory() as session:
        email = f"{uuid4()}@revision-benchmark.invalid"
        user = User(email=email, normalized_email=email, password_hash="unused")
        session.add(user)
        await session.flush()
        org = Organization(name="Revision benchmark", created_by=user.id)
        session.add(org)
        await session.flush()
        workspace = Workspace(name="Revision benchmark", organization_id=org.id)
        session.add(workspace)
        await session.commit()
        return _context(
            user_id=user.id, organization_id=org.id, workspace_id=workspace.id
        ).model_copy(
            update={
                "permissions": frozenset({"knowledge_run", "knowledge_create", "knowledge_edit"})
            }
        )


async def post(client, url, **kwargs):
    response = await client.post(url, **kwargs)
    response.raise_for_status()
    return response.json()


@dataclass
class Source:
    path: str
    content: bytes
    document: str
    revision: str
    chunks: dict

    def manifest_hash(self):
        raw = self.content.decode("utf-8-sig")
        manifest = []
        for row in sorted(self.chunks.values(), key=lambda c: c.ordinal):
            a, b = row.locator["char_start"], row.locator["char_end"]
            assert 0 <= a < b <= len(raw) and row.locator["type"] == "text_range"
            assert normalize_chunk_text(raw[a:b]) == row.text
            manifest.append((row.ordinal, row.locator, digest(row.text.encode())))
        return digest(json.dumps(manifest, sort_keys=True).encode())

    def verify(self, item):
        row = self.chunks[item["chunk_id"]]
        locator = item["locator"]
        assert (
            item["document_id"] == self.document and item["document_revision_id"] == self.revision
        )
        assert item["source"] == f"document:{self.document}/revision:{self.revision}"
        assert locator == row.locator and locator["type"] == "text_range"
        start, end = locator["char_start"], locator["char_end"]
        raw = self.content.decode("utf-8-sig")
        assert 0 <= start < end <= len(raw)
        assert normalize_chunk_text(raw[start:end]) == row.text
        assert item["snippet"] == row.text[:1000]
        return {
            "path": self.path,
            "chars": [start, end],
            "ordinal": row.ordinal,
            "sha256": digest(row.text.encode()),
        }


async def ingest(client, kb_url, content, path, prior, queue, factory, settings, components):
    url = kb_url + "/documents"
    if prior:
        url += f"/{prior.document}/revisions"
    before = (components.dense.documents, components.sparse.documents)
    started = time.perf_counter()
    uploaded = await post(client, url, files={"file": (Path(path).name, content, "text/markdown")})
    job_id = queue.jobs.popleft()
    assert str(job_id) == uploaded["ingestion_job"]["id"]
    await _process_knowledge_ingestion(job_id, settings, indexing_factory=lambda _: components)
    elapsed = time.perf_counter() - started
    revision_id = UUID(uploaded["revision"]["id"])
    async with factory() as session:
        job = await session.get(IngestionJob, job_id)
        revision = await session.get(DocumentRevision, revision_id)
        if job.status != "SUCCEEDED" or revision.ingestion_status != "READY":
            raise RuntimeError(f"Ingestion {job.status}: {job.last_error_code}")
        chunks = list(
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_revision_id == revision_id)
            )
        )
    source = Source(
        path, content, uploaded["document"]["id"], str(revision_id), {c.chunk_id: c for c in chunks}
    )
    prior_offsets = {}
    if prior:
        assert source.document == prior.document and source.revision != prior.revision
        assert source.chunks.keys().isdisjoint(prior.chunks)
        for row in prior.chunks.values():
            prior_offsets.setdefault(row.text, set()).add(row.locator["char_start"])
    matched = [c for c in chunks if c.text in prior_offsets]
    receipt = {
        "path": path,
        "chunks": len(chunks),
        "exact_previous_revision": len(matched),
        "manifest_sha256": source.manifest_hash(),
        "moved_exact": sum(c.locator["char_start"] not in prior_offsets[c.text] for c in matched),
        "dense_inputs": components.dense.documents - before[0],
        "sparse_inputs": components.sparse.documents - before[1],
        "seconds": round(elapsed, 5),
    }
    return source, receipt


async def query_snapshots(client, url, snapshots, sources):
    receipts = {}
    for version, snapshot in snapshots.items():
        by_revision = {s.revision: s for s in sources[version].values()}
        for name, path, question, expected in PROBES:
            result = await post(
                client,
                url + "/retrieval/playground",
                json={
                    "query": question,
                    "knowledge_snapshot_id": snapshot,
                    "final_top_k": TOP_K,
                },
            )
            assert result["snapshot_id"] == snapshot
            for stage in result["stages"].values():
                if stage:
                    for hit in stage["results"]:
                        source = by_revision[hit["document_revision_id"]]
                        assert source.chunks[hit["chunk_id"]].locator == hit["locator"]
            ranked = []
            gold_rank = None
            for rank, item in enumerate(result["evidence"], 1):
                source = by_revision[item["document_revision_id"]]
                ranked.append(source.verify(item))
                row = source.chunks[item["chunk_id"]]
                if (
                    not gold_rank
                    and version in expected
                    and source.path == path
                    and expected[version] in row.text
                ):
                    gold_rank = rank
            assert ranked
            receipts[f"{version}/{name}"] = {
                "gold_applicable": version in expected,
                "gold_rank": gold_rank,
                "ranking": ranked,
                "scores": [
                    {"retrieval": h["retrieval_score"], "rerank": h["rerank_score"]}
                    for h in result["evidence"]
                ],
            }
    return receipts


async def arm(factory, settings, models, corpus, chunking):
    context = await arm_context(factory)
    index = QdrantVectorIndex(
        url="",
        collection_name="revision_benchmark",
        dense_vector_size=1024,
        client=QdrantClient(":memory:"),
    )
    components = RetrievalComponents(*models, index)
    app, queue = FastAPI(), InlineQueue()
    app.include_router(router)
    app.state.db_session_factory = factory
    app.state.settings = settings
    app.dependency_overrides.update(
        {
            get_workspace_context: lambda: context,
            get_agent_run_workspace_context: lambda: context,
            get_retrieval_components: lambda: components,
            get_ingestion_queue: lambda: queue,
        }
    )
    base = f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases"
    documents, snapshots, metrics = {}, {}, {}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
        ) as client:
            kb = await post(
                client, base, json={"name": "Git revisions", "chunking_strategy": chunking}
            )
            url = base + "/" + kb["id"]
            for version, revisions in corpus.items():
                sources = {}
                metrics[version] = []
                for path, content in revisions.items():
                    sources[path], counts = await ingest(
                        client,
                        url,
                        content,
                        path,
                        documents.get("old", {}).get(path),
                        queue,
                        factory,
                        settings,
                        components,
                    )
                    metrics[version].append(counts)
                documents[version] = sources
                snapshot = await post(client, url + "/snapshots")
                assert snapshot["item_count"] == len(revisions)
                snapshots[version] = snapshot["id"]
            assert snapshots["old"] != snapshots["new"]
            # Query both *after* the revisions have changed and the old ones have retired.
            evidence = await query_snapshots(client, url, snapshots, documents)
            return {"ingestion": metrics, "queries": evidence}
    finally:
        index.close()


def summarize(trials):
    paired = []
    max_deltas = {"retrieval": 0.0, "rerank": 0.0}
    exact_rerank_ties = 0
    for trial in trials:
        comparisons = {}
        for key, full in trial["full"]["queries"].items():
            cached = trial["cache"]["queries"][key]
            comparisons[key] = full["ranking"] == cached["ranking"]
            refs = {
                (r["path"], r["ordinal"]): s
                for r, s in zip(full["ranking"], full["scores"], strict=True)
            }
            for r, s in zip(cached["ranking"], cached["scores"], strict=True):
                ref = refs.get((r["path"], r["ordinal"]))
                if ref:
                    for metric in max_deltas:
                        max_deltas[metric] = max(max_deltas[metric], abs(ref[metric] - s[metric]))
            exact_rerank_ties += any(
                a["rerank"] == b["rerank"]
                for a, b in zip(full["scores"], full["scores"][1:], strict=False)
            )
        paired.append(comparisons)
    summary = {
        "max_common_hit_score_delta": max_deltas,
        "full_queries_with_adjacent_exact_rerank_tie": exact_rerank_ties,
        "identical_rankings": sum(sum(p.values()) for p in paired),
        "compared_rankings": sum(map(len, paired)),
        "rank_agreement_by_pair": paired,
    }
    for trial in trials:
        for version in COMMITS:
            full = trial["full"]["ingestion"][version]
            cached = trial["cache"]["ingestion"][version]
            for a, b in zip(full, cached, strict=True):
                assert (a["path"], a["manifest_sha256"]) == (b["path"], b["manifest_sha256"])
                assert a["dense_inputs"] == a["sparse_inputs"] == a["chunks"]
                assert 0 <= b["dense_inputs"] <= b["chunks"]
                assert 0 <= b["sparse_inputs"] <= b["chunks"]
    for arm_name in ("full", "cache"):
        for version in COMMITS:
            times = [sum(d["seconds"] for d in t[arm_name]["ingestion"][version]) for t in trials]
            summary[f"{arm_name}_{version}_seconds"] = {
                "trials": times,
                "median": statistics.median(times),
            }
    return summary


async def run(args):
    dsn = _async_database_url(os.environ["AGENTHUB_TEST_DATABASE_URL"])
    target = make_url(dsn)
    if (
        target.host not in {"localhost", "127.0.0.1", "::1"}
        or target.database != "agenthub_reuse_benchmark"
    ):
        raise ValueError("Use a dedicated local database named agenthub_reuse_benchmark.")
    if "knowledge_vector_cache_enabled" not in Settings.model_fields:
        raise RuntimeError("This checkout does not implement the worker cache switch.")
    models = (
        CountDense(
            model_name=str(args.embedding.resolve(strict=True)),
            device=args.device,
            batch_size=args.batch_size,
        ),
        CountSparse(model_name=str(args.embedding.resolve(strict=True))),
        BgeReranker(
            model_name=str(args.reranker.resolve(strict=True)),
            device=args.device,
            batch_size=args.batch_size,
        ),
    )
    # Warm every model and perform inference before collecting either arm's times.
    models[0].embed_query("知识快照")
    models[1].encode_query("知识快照")
    models[2].rerank("知识快照", [RerankCandidate("warm", "知识快照")])
    corpus = {
        v: {p: git("show", f"{sha}:{p}") for p in (DOMAIN, DURABILITY)}
        for v, sha in COMMITS.items()
    }
    for _, path, _, expectations in PROBES:
        for version, content in corpus.items():
            for phrase in expectations.values():
                assert (phrase in content[path].decode()) == (phrase == expectations.get(version))
    identities, fingerprint_seconds = {}, {}
    for label, model in zip(("dense", "sparse"), models[:2], strict=True):
        started = time.perf_counter()
        identities[label] = model.document_cache_fingerprint()
        fingerprint_seconds[label] = round(time.perf_counter() - started, 5)
    result = {
        "worker_base_commit": git("rev-parse", "HEAD").decode().strip(),
        "sources": {
            v: {"commit": COMMITS[v], "blob_sha256": {p: digest(b) for p, b in data.items()}}
            for v, data in corpus.items()
        },
        "fingerprints": identities,
        "first_fingerprint_seconds_after_model_warmup": fingerprint_seconds,
        "runtime": {
            pkg: package_version(pkg)
            for pkg in (
                "torch",
                "transformers",
                "sentence-transformers",
                "qdrant-client",
                "asyncpg",
            )
        },
        "device": args.device,
        "batch_size": args.batch_size,
        "embedding_snapshot": args.embedding.name,
        "reranker_snapshot": args.reranker.name,
        "chunking": args.chunking,
        "chunk_overlap_chars": 200 if args.chunking == "fixed_v1" else 0,
        "chunk_size_chars": 1200,
        "qdrant": "in-process SDK local mode; unique instance per arm",
        "top_k": TOP_K,
        "probes": [{"name": n, "path": p, "query": q, "expected": e} for n, p, q, e in PROBES],
        "trials": [],
    }
    engine, factory = create_database(dsn)
    root = PROJECT_ROOT / ".scratch" / "revision-benchmark-blobs" / uuid4().hex
    root.mkdir(parents=True)
    try:
        for n in range(args.rounds):
            order = ["full", "cache"] if n % 2 == 0 else ["cache", "full"]
            trial = {"order": order}
            for name in order:
                print(f"Pair {n + 1}/{args.rounds}: {name}", flush=True)
                settings = Settings(
                    _env_file=None,
                    database_url=dsn,
                    blob_root=str(root),
                    knowledge_vector_cache_enabled=name == "cache",
                    knowledge_chunk_size_chars=1200,
                    knowledge_chunk_overlap_chars=200,
                    knowledge_min_rerank_score=None,
                )
                trial[name] = await arm(factory, settings, models, corpus, args.chunking)
            result["trials"].append(trial)
            result["summary"] = summarize(result["trials"])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    finally:
        await engine.dispose()
        for blob in root.iterdir():
            blob.unlink()
        root.rmdir()
    print(json.dumps(result["summary"], ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding", type=Path, required=True)
    parser.add_argument("--reranker", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument(
        "--chunking", choices=("structural_v1", "fixed_v1"), default="structural_v1"
    )
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.rounds, args.batch_size) < 1:
        parser.error("rounds and batch size must be positive")
    os.environ["HF_HUB_OFFLINE"] = "1"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
