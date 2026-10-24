"""Isolated, resumable SciFact runs through the production retrieval service.

Run explicit migrations before ``prepare``. This is a retrieval experiment,
not a published platform Experiment or an answer-generation evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from apps.worker.tasks.knowledge import _vector_records
from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.legacy_retriever import BaselineRetriever, OptimizedRetriever
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.control_plane.services import TenantService
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.snapshots import KnowledgeSnapshotService

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data/external/rag-public-20261021/scifact/extracted/scifact"
DATABASE = "agenthub_rag_scifact_20261021"
COLLECTION = "rag_scifact_20261021"


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def settings() -> Settings:
    # Explicitly isolate both stores; never inherit the production collection/DB.
    return Settings(
        _env_file=None,
        database_url=f"postgresql+asyncpg://agenthub:agenthub@localhost:5432/{DATABASE}",
        qdrant_url="http://localhost:6333",
        knowledge_qdrant_collection=COLLECTION,
        knowledge_dense_vector_size=1024,
        knowledge_embedding_model="BAAI/bge-m3",
        knowledge_reranker_model="BAAI/bge-reranker-v2-m3",
        knowledge_embedding_device="cuda",
        knowledge_reranker_device="cuda",
        knowledge_embedding_batch_size=8,
        knowledge_reranker_batch_size=8,
        knowledge_qdrant_timeout_seconds=120,
    )


def offline_models() -> None:
    os.environ.setdefault("HF_HOME", str(ROOT.parent / "hf_cache"))
    # The existing cache is itself a hub directory, not HF_HOME/hub.
    os.environ.setdefault("HF_HUB_CACHE", str(ROOT.parent / "hf_cache"))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"


def retrieval_identity(configuration: Settings) -> dict:
    return {
        "embedding_model": configuration.knowledge_embedding_model,
        "reranker_model": configuration.knowledge_reranker_model,
        "dense_dimension": configuration.knowledge_dense_vector_size,
        "embedding_batch_size": configuration.knowledge_embedding_batch_size,
        "reranker_batch_size": configuration.knowledge_reranker_batch_size,
        "qdrant_url": configuration.qdrant_url,
        "collection": configuration.knowledge_qdrant_collection,
        "chunk_size_chars": 1200,
        "chunk_overlap_chars": 200,
        "text_policy": "stripped title + two newlines + stripped abstract",
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "final_top_k": 20,
        "rrf_k": 60,
    }


async def authorized_context(session, user_id: str, workspace_id: str):
    access = await TenantService().get_workspace_access(
        session,
        principal=PrincipalContext(
            request_id="scifact-benchmark",
            trace_id="scifact-benchmark",
            user_id=user_id,
        ),
        workspace_id=UUID(workspace_id),
    )
    return access.context


async def prepare(args) -> None:
    source = load_scifact(args.data, "train")
    configuration = settings()
    engine, factory = create_database(configuration.database_url)
    started = time.perf_counter()
    try:
        if args.state.exists():
            raise ValueError("state already exists; use the prepared index or a fresh explicit run")
        async with factory() as session:
            user = User(
                id=uuid4(),
                email=f"scifact-{uuid4()}@benchmark.invalid",
                normalized_email=f"scifact-{uuid4()}@benchmark.invalid",
                password_hash="benchmark-only",
            )
            session.add(user)
            await session.flush()
            organization = Organization(id=uuid4(), name="SciFact isolated", created_by=user.id)
            session.add(organization)
            await session.flush()
            session.add(
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=user.id,
                    role="OWNER",
                )
            )
            workspace = Workspace(id=uuid4(), organization_id=organization.id, name="SciFact")
            session.add(workspace)
            await session.flush()
            kb = KnowledgeBase(id=uuid4(), workspace_id=workspace.id, name="SciFact full corpus")
            session.add(kb)
            await session.flush()
            documents, revisions, chunks = [], [], []
            chunk_sources = {}
            for doc in source.corpus.values():
                document_id, revision_id = uuid4(), uuid4()
                text = doc.retrieval_text
                documents.append(
                    Document(
                        id=document_id,
                        workspace_id=workspace.id,
                        knowledge_base_id=kb.id,
                        name=f"scifact:{doc.document_id}",
                    )
                )
                revisions.append(
                    DocumentRevision(
                        id=revision_id,
                        workspace_id=workspace.id,
                        knowledge_base_id=kb.id,
                        document_id=document_id,
                        revision_number=1,
                        original_filename=f"{doc.document_id}.txt",
                        blob_key=f"benchmark/scifact/{revision_id}",
                        media_type="text/plain",
                        file_size=len(text.encode("utf-8")),
                        ingestion_status=RevisionIngestionStatus.READY,
                        lifecycle_status=RevisionLifecycleStatus.ACTIVE,
                    )
                )
                parsed = ParsedDocument((ParsedBlock(text, {"type": "text_range"}),))
                for candidate in build_deterministic_chunks(
                    revision_id,
                    parsed,
                    chunk_size_chars=1200,
                    chunk_overlap_chars=200,
                ):
                    chunks.append(
                        DocumentChunk(
                            id=uuid4(),
                            chunk_id=candidate.chunk_id,
                            workspace_id=workspace.id,
                            knowledge_base_id=kb.id,
                            document_id=document_id,
                            document_revision_id=revision_id,
                            ordinal=candidate.ordinal,
                            normalized_content_hash=candidate.normalized_content_hash,
                            text=candidate.text,
                            locator=candidate.locator,
                        )
                    )
                    chunk_sources[candidate.chunk_id] = doc.document_id
            session.add_all(documents)
            await session.flush()
            session.add_all(revisions)
            await session.flush()
            session.add_all(chunks)
            await session.commit()
            context = await authorized_context(session, str(user.id), str(workspace.id))
            snapshot = await KnowledgeSnapshotService().create_current_snapshot(
                session,
                context,
                kb.id,
            )
        print(f"seeded documents={len(documents)} chunks={len(chunks)}", flush=True)
        components = production_retrieval_components(configuration)
        index_model_load_ms = {}
        for name, component in (("dense", components.dense), ("sparse", components.sparse)):
            load_started = time.perf_counter()
            await asyncio.to_thread(component.warm)
            index_model_load_ms[name] = (time.perf_counter() - load_started) * 1000
        index_started = time.perf_counter()
        components.index.ensure_collection()
        claim = SimpleNamespace(workspace_id=workspace.id, knowledge_base_id=kb.id)
        for offset in range(0, len(chunks), 128):
            records = await asyncio.to_thread(
                _vector_records,
                claim,
                chunks[offset : offset + 128],
                components.dense,
                components.sparse,
            )
            await asyncio.to_thread(components.index.upsert, records)
            print(f"indexed {min(offset + 128, len(chunks))}/{len(chunks)}", flush=True)
        dump(
            args.state,
            {
                "schema_version": 1,
                "database": DATABASE,
                "collection": COLLECTION,
                "created_at": datetime.now(UTC).isoformat(),
                "user_id": str(user.id),
                "organization_id": str(organization.id),
                "workspace_id": str(workspace.id),
                "knowledge_base_id": str(kb.id),
                "snapshot_id": str(snapshot.snapshot_id),
                "snapshot_hash": snapshot.content_hash,
                "documents": len(documents),
                "chunks": len(chunks),
                "retrieval_identity": retrieval_identity(configuration),
                "chunk_sources": chunk_sources,
                "text_policy": "stripped title + two newlines + stripped abstract",
                "chunk_size_chars": 1200,
                "chunk_overlap_chars": 200,
                "index_model_load_ms": index_model_load_ms,
                "index_ms": (time.perf_counter() - index_started) * 1000,
                "prepare_ms": (time.perf_counter() - started) * 1000,
            },
        )
        components.index.close()
    finally:
        await engine.dispose()


async def run(args) -> None:
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if state["database"] != DATABASE or state["collection"] != COLLECTION:
        raise ValueError("state does not belong to the isolated benchmark stores")
    if args.output.exists():
        raise ValueError("refusing to overwrite a historical run")
    if args.rrf_k != 60:
        raise ValueError("this milestone fixes RRF=60; no unregistered tuning")
    if args.split == "test":
        if args.limit:
            raise ValueError("final test must cover every labeled test query")
        if not args.frozen or not args.frozen.is_file():
            raise ValueError("test requires a frozen DEV selection file")
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if (
            frozen["baseline_implementation"] != "legacy_revision_in"
            or frozen["selected_implementation"] != "snapshot_membership_join"
            or frozen["rrf_k"] != 60
            or frozen["seed"] != args.seed
            or frozen["snapshot_id"] != state["snapshot_id"]
            or frozen["controls"] != state["retrieval_identity"]
        ):
            raise ValueError("test configuration differs from the frozen DEV selection")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.with_name("test-exposures.jsonl").open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "event": "TEST_LABEL_ACCESS",
                        "at": datetime.now(UTC).isoformat(),
                        "output": args.output.name,
                        "selection": frozen,
                    }
                )
                + "\n"
            )
    source = load_scifact(args.data, args.split)
    configuration = settings()
    identity = retrieval_identity(configuration)
    if identity != state["retrieval_identity"]:
        raise ValueError("prepared index and run retrieval identity differ")
    engine, factory = create_database(configuration.database_url)
    components = None
    handles = {}
    try:
        async with factory() as session:
            context = await authorized_context(session, state["user_id"], state["workspace_id"])
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session,
                context,
                UUID(state["knowledge_base_id"]),
                state["snapshot_id"],
            )
            if (
                snapshot.content_hash != state["snapshot_hash"]
                or snapshot.item_count != state["documents"]
            ):
                raise ValueError("prepared snapshot identity differs from its persisted state")
        query_ids = sorted(source.queries)
        random.Random(args.seed).shuffle(query_ids)
        if args.limit:
            query_ids = query_ids[: args.limit]
        names = ("baseline", "optimized") if args.paired else (args.implementation,)
        outputs = {
            name: args.output.with_name(args.output.stem + "-" + name + args.output.suffix)
            if args.paired
            else args.output
            for name in names
        }
        if any(path.exists() for path in outputs.values()):
            raise ValueError("refusing to overwrite a historical paired run")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        components = production_retrieval_components(configuration)
        load_times = {}
        for name, component in (
            ("dense", components.dense),
            ("sparse", components.sparse),
            ("reranker", components.reranker),
        ):
            begin = time.perf_counter()
            await asyncio.to_thread(component.warm)
            load_times[name] = (time.perf_counter() - begin) * 1000
        import torch

        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True))
        retrievers = {}
        for name, output in outputs.items():
            cls = BaselineRetriever if name == "baseline" else OptimizedRetriever
            retrievers[name] = cls(
                session_factory=factory,
                dense_embedder=components.dense,
                sparse_encoder=components.sparse,
                reranker=components.reranker,
                vector_index=components.index,
                rrf_k=60,
            )
            dump(
                output.with_suffix(".meta.json"),
                {
                    "schema_version": 2,
                    "benchmark": "scifact-production-retrieval",
                    "split": args.split,
                    "queries": len(query_ids),
                    "seed": args.seed,
                    "rrf_k": 60,
                    "dense_top_k": 30,
                    "sparse_top_k": 30,
                    "candidate_top_k": 20,
                    "final_top_k": 20,
                    "documents": state["documents"],
                    "chunks": state["chunks"],
                    "snapshot_id": state["snapshot_id"],
                    "snapshot_hash": state["snapshot_hash"],
                    "embedding_model": configuration.knowledge_embedding_model,
                    "reranker_model": configuration.knowledge_reranker_model,
                    "reranker_batch_size": configuration.knowledge_reranker_batch_size,
                    "retrieval_identity": identity,
                    "implementation": "legacy_revision_in"
                    if name == "baseline"
                    else "snapshot_membership_join",
                    "paired_interleaved": args.paired,
                    "sparse": "tokenizer log-TF + Qdrant IDF; not learned BGE sparse or BM25",
                    "torch": torch.__version__,
                    "device": torch.cuda.get_device_name(0),
                    "cold_load_ms": load_times,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "git_commit": commit,
                    "git_dirty": dirty,
                    "scope": "retrieval-only; no generation or API model charges",
                    "latency_policy": "concurrency=1; paired randomized order; exclude first query",
                },
            )
            handles[name] = output.open("x", encoding="utf-8")
        order_rng = random.Random(args.seed + 23)
        for ordinal, query_id in enumerate(query_ids):
            order = list(names)
            order_rng.shuffle(order)
            for name in order:
                retriever = retrievers[name]
                started = time.perf_counter()
                result = await retriever.retrieve_with_trace(
                    context,
                    RetrievalQuery(
                        text=source.queries[query_id],
                        knowledge_base_id=state["knowledge_base_id"],
                        knowledge_snapshot_id=state["snapshot_id"],
                        strategy=RetrievalStrategy.HYBRID_RERANK,
                        dense_top_k=30,
                        sparse_top_k=30,
                        candidate_top_k=20,
                        final_top_k=20,
                    ),
                )
                total = (time.perf_counter() - started) * 1000
                stages = {}
                for stage_name in ("dense", "sparse", "fusion", "rerank"):
                    stage = getattr(result.trace, stage_name)
                    stages[stage_name] = {
                        "latency_ms": stage.latency_ms,
                        "ranking": [
                            {
                                "chunk_id": hit.chunk_id,
                                "document_id": state["chunk_sources"][hit.chunk_id],
                                "score": hit.score,
                            }
                            for hit in stage.results
                        ],
                    }
                handles[name].write(
                    json.dumps(
                        {
                            "query_id": query_id,
                            "ordinal": ordinal,
                            "first_inference": ordinal == 0,
                            "pair_order": order,
                            "total_ms": total,
                            "stages": stages,
                            "db_ms": {
                                "snapshot_scope": retriever.snapshot_scope_ms,
                                "chunk_load": retriever.chunk_load_ms,
                            },
                        }
                    )
                    + "\n"
                )
                handles[name].flush()
                print(
                    f"query {ordinal + 1}/{len(query_ids)} id={query_id} {name} ms={total:.1f}",
                    flush=True,
                )
    finally:
        for handle in handles.values():
            handle.close()
        if components is not None:
            components.index.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--split", choices=("train", "test"), default="train")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20261021)
    parser.add_argument("--rrf-k", type=int, choices=(60,), default=60)
    parser.add_argument("--paired", action="store_true")
    parser.add_argument("--implementation", choices=("baseline", "optimized"), default="optimized")
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    if args.rrf_k < 1 or args.limit < 0:
        parser.error("invalid positive limits")
    if args.action == "run" and args.output is None:
        parser.error("run requires --output")
    offline_models()
    asyncio.run(prepare(args) if args.action == "prepare" else run(args))


if __name__ == "__main__":
    main()
