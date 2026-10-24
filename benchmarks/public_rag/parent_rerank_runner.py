"""Run a fixed parent-context mechanism through the authorized production retriever.

Public-corpus parent preloading is an experimental adapter, not production caching.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from benchmarks.public_rag.budget_ablation import exposure
from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.legacy_retriever import OptimizedRetriever
from benchmarks.public_rag.parent_rerank_adapter import ParentContextReranker
from benchmarks.public_rag.scifact_runner import (
    COLLECTION,
    DATA,
    DATABASE,
    authorized_context,
    dump,
    offline_models,
    retrieval_identity,
    settings,
)
from packages.core.database import create_database
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.models import DocumentChunk, KnowledgeSnapshotItem
from packages.knowledge.snapshots import KnowledgeSnapshotService

ARMS = {"baseline": "chunk", "parent": "parent"}
BUDGETS = {
    "dense_top_k": 30,
    "sparse_top_k": 30,
    "candidate_top_k": 20,
    "rerank_top_k": 20,
    "final_top_k": 20,
    "rrf_k": 60,
}


def verified_parents(rows, state, corpus):
    """Bind source parent text to complete, snapshot-authorized DB child ranges."""
    mapping, coverage, parent_texts = {}, defaultdict(list), {}
    for child in rows:
        upstream = state["chunk_sources"].get(child.chunk_id)
        if upstream not in corpus or child.chunk_id in mapping:
            raise ValueError("unknown or duplicate authorized child")
        text = normalize_chunk_text(corpus[upstream].retrieval_text)
        start, end = child.locator.get("char_start"), child.locator.get("char_end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
            or normalize_chunk_text(text[start:end]) != child.text
        ):
            raise ValueError("source parent differs from persisted child text/range")
        parent_id = str(child.document_revision_id)
        if parent_id in parent_texts and parent_texts[parent_id] != (upstream, text):
            raise ValueError("conflicting parent revision source")
        parent_texts[parent_id] = (upstream, text)
        coverage[parent_id].append((start, end))
        mapping[child.chunk_id] = (parent_id, text)
    if set(mapping) != set(state["chunk_sources"]) or len(parent_texts) != state["documents"]:
        raise ValueError("snapshot parent/child inventory differs from prepared state")
    if len({source for source, _ in parent_texts.values()}) != len(parent_texts):
        raise ValueError("this benchmark requires one frozen revision per source document")
    for parent_id, intervals in coverage.items():
        covered = 0
        for start, end in sorted(intervals):
            if start > covered:
                raise ValueError("parent source has an uncovered range")
            covered = max(covered, end)
        if covered != len(parent_texts[parent_id][1]):
            raise ValueError("parent source has an uncovered suffix")
    return mapping


def token_audit(tokenizer, query, pairs, *, batch_size=8):
    if not pairs:
        return {
            "encoded_tokens": 0,
            "padded_tokens": 0,
            "truncated_pairs": 0,
            "max_untruncated_tokens": 0,
        }
    inputs = ([query] * len(pairs), [pair.text for pair in pairs])
    full = tokenizer(*inputs, padding=False, truncation=False, verbose=False)["input_ids"]
    encoded = tokenizer(*inputs, padding=False, truncation=True, verbose=False)["input_ids"]
    return {
        "encoded_tokens": sum(map(len, encoded)),
        "padded_tokens": sum(
            max(map(len, encoded[offset : offset + batch_size]))
            * len(encoded[offset : offset + batch_size])
            for offset in range(0, len(encoded), batch_size)
        ),
        "truncated_pairs": sum(len(a) > len(b) for a, b in zip(full, encoded, strict=True)),
        "max_untruncated_tokens": max(map(len, full)),
    }


async def run(args):
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if protocol["arms"] != ARMS or any(protocol[k] != v for k, v in BUDGETS.items()):
        raise ValueError("protocol differs from the registered mechanism")
    if (
        state["database"] != DATABASE
        or state["collection"] != COLLECTION
        or state["snapshot_id"] != protocol["snapshot_id"]
    ):
        raise ValueError("state must refer to the isolated prepared snapshot")
    if args.phase == "regression":
        if not args.frozen:
            raise ValueError("regression requires a passing frozen DEV selection")
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if frozen["protocol"] != protocol or frozen["selected_arm"] != "parent":
            raise ValueError("do not spend regression labels on a rejected candidate")
    paths = {name: args.output / f"{args.phase}-{name}.jsonl" for name in ARMS}
    if any(path.exists() or path.with_suffix(".meta.json").exists() for path in paths.values()):
        raise ValueError("refusing to overwrite historical traces")
    exposure(
        args.output,
        "DEV_LABEL_ACCESS" if args.phase == "dev" else "EXPOSED_TEST_REGRESSION_LABEL_ACCESS",
        protocol=str(args.protocol),
    )
    split = "train" if args.phase == "dev" else "test"
    source = load_scifact(args.data, split)
    config = settings()
    identity = retrieval_identity(config)
    if identity != state["retrieval_identity"]:
        raise ValueError("prepared retrieval controls differ")
    engine, factory = create_database(config.database_url)
    components, handles = None, {}
    try:
        async with factory() as session:
            context = await authorized_context(session, state["user_id"], state["workspace_id"])
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session, context, UUID(state["knowledge_base_id"]), state["snapshot_id"]
            )
            if snapshot.content_hash != state["snapshot_hash"]:
                raise ValueError("persisted snapshot differs")
            chunks = list(
                await session.scalars(
                    select(DocumentChunk)
                    .join(
                        KnowledgeSnapshotItem,
                        (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
                        & (
                            KnowledgeSnapshotItem.knowledge_base_id
                            == DocumentChunk.knowledge_base_id
                        )
                        & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
                        & (
                            KnowledgeSnapshotItem.document_revision_id
                            == DocumentChunk.document_revision_id
                        ),
                    )
                    .where(
                        DocumentChunk.workspace_id == UUID(state["workspace_id"]),
                        DocumentChunk.knowledge_base_id == UUID(state["knowledge_base_id"]),
                        KnowledgeSnapshotItem.snapshot_id == UUID(state["snapshot_id"]),
                    )
                )
            )
            parents = verified_parents(chunks, state, source.corpus)
        components = production_retrieval_components(config)
        cold = {}
        for name, component in (
            ("dense", components.dense),
            ("sparse", components.sparse),
            ("reranker", components.reranker),
        ):
            start = time.perf_counter()
            await asyncio.to_thread(component.warm)
            cold[name] = (time.perf_counter() - start) * 1000
        wrappers = {
            name: ParentContextReranker(components.reranker, parents=parents, mode=mode)
            for name, mode in ARMS.items()
        }
        retrievers = {
            name: OptimizedRetriever(
                session_factory=factory,
                dense_embedder=components.dense,
                sparse_encoder=components.sparse,
                reranker=wrappers[name],
                vector_index=components.index,
                rrf_k=60,
            )
            for name in ARMS
        }
        query_ids = sorted(source.queries)
        random.Random(protocol["seed"]).shuffle(query_ids)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        for name, path in paths.items():
            dump(
                path.with_suffix(".meta.json"),
                {
                    "protocol": protocol,
                    "arm": name,
                    "phase": args.phase,
                    "split": split,
                    "queries": len(query_ids),
                    "snapshot_id": state["snapshot_id"],
                    "retrieval_identity": identity,
                    "paired_interleaved": True,
                    "cold_load_ms": cold,
                    "git_commit": commit,
                    "git_dirty": True,
                    "started_at": datetime.now(UTC).isoformat(),
                    "scope": (
                        "public retrieval prototype; parent RAM loading excluded; "
                        "no Formal Experiment"
                    ),
                },
            )
            handles[name] = path.open("x", encoding="utf-8")
        rng = random.Random(protocol["seed"] + 23)
        for ordinal, qid in enumerate(query_ids):
            order = list(ARMS)
            rng.shuffle(order)
            pending = {}
            for name in order:
                retriever = retrievers[name]
                start = time.perf_counter()
                result = await retriever.retrieve_with_trace(
                    context,
                    RetrievalQuery(
                        text=source.queries[qid],
                        knowledge_base_id=state["knowledge_base_id"],
                        knowledge_snapshot_id=state["snapshot_id"],
                        strategy=RetrievalStrategy.HYBRID_RERANK,
                        dense_top_k=30,
                        sparse_top_k=30,
                        candidate_top_k=20,
                        rerank_top_k=20,
                        final_top_k=20,
                    ),
                )
                elapsed = (time.perf_counter() - start) * 1000
                stages = {}
                for stage_name in ("dense", "sparse", "fusion", "selection", "rerank"):
                    stage = getattr(result.trace, stage_name)
                    if stage is None:
                        raise ValueError("actual rerank input trace is required")
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
                pending[name] = {
                    "query_id": qid,
                    "ordinal": ordinal,
                    "first_inference": ordinal == 0,
                    "pair_order": order,
                    "total_ms": elapsed,
                    "stages": stages,
                    "db_ms": {
                        "snapshot_scope": retriever.snapshot_scope_ms,
                        "chunk_load": retriever.chunk_load_ms,
                    },
                    "compute": dict(wrappers[name].last_stats),
                }
            # Audit the exact texts sent to the same tokenizer after BOTH timed calls.
            for name in order:
                pending[name]["compute"].update(
                    token_audit(
                        components.reranker._tokenizer,
                        source.queries[qid],
                        wrappers[name].last_pairs,
                    )
                )
                handles[name].write(json.dumps(pending[name]) + "\n")
                handles[name].flush()
            if ordinal == 0 or (ordinal + 1) % 50 == 0 or ordinal + 1 == len(query_ids):
                print(f"{args.phase} query {ordinal + 1}/{len(query_ids)}", flush=True)
    finally:
        for handle in handles.values():
            handle.close()
        if components is not None:
            components.index.close()
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("dev", "regression"))
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    offline_models()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
