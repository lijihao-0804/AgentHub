"""Budget/diversity ablation over the production retriever, without generation.

DEV scores each union candidate once. Subset replay is quality screening only;
the regression phase actually executes every declared arm in randomized order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.legacy_retriever import OptimizedRetriever
from benchmarks.public_rag.scifact_runner import (
    DATA,
    authorized_context,
    dump,
    offline_models,
    retrieval_identity,
    settings,
)
from packages.core.database import create_database
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.retrieval import _FusedCandidate, select_rerank_candidates
from packages.knowledge.snapshots import KnowledgeSnapshotService

ARMS = {
    "baseline": {"candidate_top_k": 20, "rerank_top_k": 20, "max_chunks_per_document": None},
    "diversity20": {"candidate_top_k": 60, "rerank_top_k": 20, "max_chunks_per_document": 2},
    "budget40": {"candidate_top_k": 60, "rerank_top_k": 40, "max_chunks_per_document": None},
    "combined40": {"candidate_top_k": 60, "rerank_top_k": 40, "max_chunks_per_document": 2},
    "budget60": {"candidate_top_k": 60, "rerank_top_k": 60, "max_chunks_per_document": None},
}


def replay_arm(row: dict, arm: dict, *, final_top_k: int = 20) -> dict:
    """Replay independent cross-encoder scores; deliberately omit cached timing."""
    pool = row["stages"]["fusion"]["ranking"][: arm["candidate_top_k"]]
    scores = {hit["chunk_id"]: hit["score"] for hit in row["stages"]["rerank"]["ranking"]}
    documents = {hit["chunk_id"]: hit["document_id"] for hit in pool}
    if any(hit["chunk_id"] not in scores for hit in pool):
        raise ValueError("screening requires every fused candidate to have a rerank score")
    fused = tuple(
        _FusedCandidate(hit["chunk_id"], hit["score"], rank) for rank, hit in enumerate(pool, 1)
    )
    selected = select_rerank_candidates(
        fused,
        document_ids=documents,
        limit=arm["rerank_top_k"],
        max_chunks_per_document=arm["max_chunks_per_document"],
    )
    ranked = sorted(
        selected, key=lambda hit: (-scores[hit.chunk_id], -hit.retrieval_score, hit.chunk_id)
    )[:final_top_k]
    return {
        "query_id": row["query_id"],
        "stages": {
            "fusion": {"ranking": pool},
            "selection": {
                "ranking": [
                    {
                        "chunk_id": hit.chunk_id,
                        "document_id": documents[hit.chunk_id],
                        "score": hit.retrieval_score,
                    }
                    for hit in selected
                ]
            },
            "rerank": {
                "ranking": [
                    {
                        "chunk_id": hit.chunk_id,
                        "document_id": documents[hit.chunk_id],
                        "score": scores[hit.chunk_id],
                    }
                    for hit in ranked
                ]
            },
        },
    }


def exposure(output: Path, event: str, **fields) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "label-access.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": event, "at": datetime.now(UTC).isoformat(), **fields}) + "\n"
        )


async def run(args) -> None:
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol["arms"] != ARMS or protocol["final_top_k"] != 20:
        raise ValueError("declared protocol differs from implemented arms")
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if state["snapshot_id"] != protocol["snapshot_id"]:
        raise ValueError("protocol snapshot differs")
    if args.phase == "regression":
        if not args.frozen:
            raise ValueError("regression requires a frozen DEV selection")
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if frozen["protocol"] != protocol or frozen["selected_arm"] not in ARMS:
            raise ValueError("frozen protocol differs")
    names = ("superset",) if args.phase == "screen" else tuple(ARMS)
    paths = {name: args.output / f"{args.phase}-{name}.jsonl" for name in names}
    if any(p.exists() or p.with_suffix(".meta.json").exists() for p in paths.values()):
        raise ValueError("refusing to overwrite historical run")
    exposure(
        args.output,
        "DEV_LABEL_ACCESS" if args.phase == "screen" else "EXPOSED_TEST_REGRESSION_LABEL_ACCESS",
        protocol=str(args.protocol),
    )
    split = "train" if args.phase == "screen" else "test"
    source = load_scifact(args.data, split)
    configuration = settings()
    identity = retrieval_identity(configuration)
    if identity != state["retrieval_identity"]:
        raise ValueError("prepared index controls differ")
    engine, factory = create_database(configuration.database_url)
    components, handles = None, {}
    try:
        async with factory() as session:
            context = await authorized_context(session, state["user_id"], state["workspace_id"])
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session, context, UUID(state["knowledge_base_id"]), state["snapshot_id"]
            )
            if (
                snapshot.content_hash != state["snapshot_hash"]
                or snapshot.item_count != state["documents"]
            ):
                raise ValueError("prepared snapshot differs from persisted snapshot")
        components = production_retrieval_components(configuration)
        cold = {}
        for name, component in (
            ("dense", components.dense),
            ("sparse", components.sparse),
            ("reranker", components.reranker),
        ):
            start = time.perf_counter()
            await asyncio.to_thread(component.warm)
            cold[name] = (time.perf_counter() - start) * 1000
        import torch

        retriever = OptimizedRetriever(
            session_factory=factory,
            dense_embedder=components.dense,
            sparse_encoder=components.sparse,
            reranker=components.reranker,
            vector_index=components.index,
            rrf_k=60,
        )
        query_ids = sorted(source.queries)
        random.Random(protocol["seed"]).shuffle(query_ids)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        for name, path in paths.items():
            arm = ARMS["budget60"] if name == "superset" else ARMS[name]
            dump(
                path.with_suffix(".meta.json"),
                {
                    "schema_version": 3,
                    "phase": args.phase,
                    "split": split,
                    "queries": len(query_ids),
                    "arm": name,
                    "controls": arm,
                    "final_top_k": 60 if name == "superset" else 20,
                    "snapshot_id": state["snapshot_id"],
                    "retrieval_identity": identity,
                    "protocol": protocol,
                    "cold_load_ms": cold,
                    "git_commit": commit,
                    "git_dirty": True,
                    "device": torch.cuda.get_device_name(0),
                    "torch": torch.__version__,
                    "started_at": datetime.now(UTC).isoformat(),
                    "scope": (
                        "retrieval only; test already exposed; not a platform Formal Experiment"
                    ),
                    "paired_interleaved": args.phase == "regression",
                },
            )
            handles[name] = path.open("x", encoding="utf-8")
        rng = random.Random(protocol["seed"] + 23)
        for ordinal, query_id in enumerate(query_ids):
            order = list(names)
            rng.shuffle(order)
            for name in order:
                arm = ARMS["budget60"] if name == "superset" else ARMS[name]
                start = time.perf_counter()
                result = await retriever.retrieve_with_trace(
                    context,
                    RetrievalQuery(
                        text=source.queries[query_id],
                        knowledge_base_id=state["knowledge_base_id"],
                        knowledge_snapshot_id=state["snapshot_id"],
                        strategy=RetrievalStrategy.HYBRID_RERANK,
                        dense_top_k=30,
                        sparse_top_k=30,
                        final_top_k=60 if name == "superset" else 20,
                        **arm,
                    ),
                )
                elapsed = (time.perf_counter() - start) * 1000
                stages = {}
                for stage_name in ("dense", "sparse", "fusion", "selection", "rerank"):
                    stage = getattr(result.trace, stage_name)
                    if stage is None:
                        raise ValueError("actual rerank input selection trace is required")
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
                            "total_ms": elapsed,
                            "stages": stages,
                            "output_characters": sum(len(hit.text) for hit in result.evidence),
                            "db_ms": {
                                "snapshot_scope": retriever.snapshot_scope_ms,
                                "chunk_load": retriever.chunk_load_ms,
                            },
                        }
                    )
                    + "\n"
                )
                handles[name].flush()
            if ordinal == 0 or (ordinal + 1) % 25 == 0 or ordinal + 1 == len(query_ids):
                print(f"{args.phase} query {ordinal + 1}/{len(query_ids)}", flush=True)
    finally:
        for handle in handles.values():
            handle.close()
        if components is not None:
            components.index.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("screen", "regression"))
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
