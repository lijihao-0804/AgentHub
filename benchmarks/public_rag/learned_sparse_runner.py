"""Isolated official learned-sparse mechanism experiment; production defaults stay fixed."""

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

from benchmarks.public_rag.budget_ablation import exposure
from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.learned_sparse_adapter import (
    LearnedM3SparseEncoder,
    LearnedSparseIndex,
    SharedDenseTreatmentIndex,
    iter_dense_records,
)
from benchmarks.public_rag.legacy_retriever import OptimizedRetriever
from benchmarks.public_rag.scifact_runner import (
    DATA,
    authorized_context,
    dump,
    offline_models,
    settings,
)
from packages.core.database import create_database
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.contracts import RetrievalQuery, VectorRecord, VectorScope
from packages.knowledge.snapshots import KnowledgeSnapshotService

MODEL_DIRECTORY = Path(
    "E:/JAVA/AI+agent/hf_cache/models--BAAI--bge-m3/snapshots/"
    "5617a9f61b028005a4858fdac845db406aefb181"
)
COLLECTION = "rag_scifact_learned_20261022"
ARMS = ("baseline", "learned")


def pinned_settings():
    # The existing offline main ref was checked against this cached revision.
    # Pin both query arms to the local snapshot containing the paired sparse head.
    return settings().model_copy(update={"knowledge_embedding_model": str(MODEL_DIRECTORY)})


def common_controls() -> dict:
    return {
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "rerank_top_k": 20,
        "final_top_k": 20,
        "rrf_k": 60,
        "dense_model": "BAAI/bge-m3",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "embedding_batch_size": 8,
        "reranker_batch_size": 8,
        "chunk_size_chars": 1200,
        "chunk_overlap_chars": 200,
        "max_chunks_per_document": None,
        "dense_search_policy": "Both arms use the baseline dense ANN graph",
    }


def validate_protocol(protocol: dict, state: dict) -> None:
    if protocol["snapshot_id"] != state["snapshot_id"]:
        raise ValueError("protocol snapshot mismatch")
    for key in (
        "dense_top_k",
        "sparse_top_k",
        "candidate_top_k",
        "rerank_top_k",
        "final_top_k",
        "rrf_k",
    ):
        if protocol[key] != common_controls()[key]:
            raise ValueError("protocol budgets differ from fixed mechanism comparison")
    if protocol["arms"]["learned"]["collection"] != COLLECTION:
        raise ValueError("learned experiment must use its dedicated collection")
    if protocol["arms"]["baseline"]["collection"] != "rag_scifact_20261021":
        raise ValueError("baseline collection differs")
    if protocol["arms"]["learned"]["modifier"] != "none":
        raise ValueError("learned sparse requires unmodified dot product")


def numerical_gate(dense, sparse: LearnedM3SparseEncoder) -> dict:
    """Compare the ST path to direct backbone + official max-scatter reference."""
    import torch
    import torch.nn.functional as functional

    texts = [
        "A gene gene regulates DNA repair.",
        "不同语言的稀疏词项权重也需要上下文。",
        "The repeated gene is not the same as an unrelated protein receptor " * 5,
    ]
    sparse.warm()
    model = dense._load_model()
    model.eval()
    features = {
        key: value.to(model.device) if torch.is_tensor(value) else value
        for key, value in model.tokenize(texts).items()
    }
    backbone_features = {
        key: value
        for key, value in features.items()
        if key in {"input_ids", "attention_mask", "token_type_ids"} and torch.is_tensor(value)
    }
    with torch.inference_mode():
        hidden = model[0].auto_model(**backbone_features, return_dict=True).last_hidden_state
        pipeline = model(dict(features))
        token_max_error = float((pipeline["token_embeddings"] - hidden).abs().max())
        if not torch.allclose(pipeline["token_embeddings"], hidden, rtol=0.005, atol=0.001):
            raise ValueError("ST token embeddings differ from the official backbone path")
        state = torch.load(
            MODEL_DIRECTORY / "sparse_linear.pt", map_location="cpu", weights_only=True
        )
        weights = torch.relu(
            functional.linear(hidden, state["weight"].to(hidden), state["bias"].to(hidden))
        ).squeeze(-1)
        weights = weights.masked_fill(features["attention_mask"] == 0, 0)
        reference = torch.zeros(
            len(texts), model.tokenizer.vocab_size, dtype=weights.dtype, device=weights.device
        )
        reference.scatter_reduce_(1, features["input_ids"], weights, reduce="amax")
        excluded = [
            model.tokenizer.cls_token_id,
            model.tokenizer.eos_token_id,
            model.tokenizer.pad_token_id,
            model.tokenizer.unk_token_id,
        ]
        reference[:, excluded] = 0
        reference = reference.float().cpu()
        cls = functional.normalize(hidden[:, 0].float(), p=2, dim=1).cpu()
    actual = sparse.encode_documents(texts)
    max_weight_error = 0.0
    for ordinal, encoded in enumerate(actual):
        indices = tuple(reference[ordinal].nonzero().reshape(-1).tolist())
        if indices != encoded.indices:
            raise ValueError("learned sparse coordinates differ from official max reference")
        values = torch.tensor(encoded.values, dtype=torch.float32)
        expected = reference[ordinal, list(indices)]
        if values.numel():
            max_weight_error = max(max_weight_error, float((values - expected).abs().max()))
        if not torch.allclose(values, expected, rtol=0.005, atol=0.001):
            raise ValueError("learned sparse weights differ from official reference")
    old_dense = torch.tensor(dense.embed_documents(texts), dtype=torch.float32)
    cosine = functional.cosine_similarity(old_dense, cls, dim=1)
    if float(cosine.min()) < 0.99999:
        raise ValueError("reference dense CLS differs from legacy dense model")
    return {
        "status": "PASS",
        "fixtures": len(texts),
        "device": str(model.device),
        "dtype": str(hidden.dtype),
        "model_directory": str(MODEL_DIRECTORY),
        "token_max_abs_error": token_max_error,
        "sparse_max_abs_error": max_weight_error,
        "dense_min_cosine": float(cosine.min()),
        "sparse_nnz": [len(value.indices) for value in actual],
        "reference": "direct backbone; ReLU(linear); vocabulary scatter_reduce amax",
        "rtol": 0.005,
        "atol": 0.001,
        "no_flagembedding_dependency": True,
    }


async def checked_context(factory, state: dict):
    async with factory() as session:
        context = await authorized_context(session, state["user_id"], state["workspace_id"])
        snapshot = await KnowledgeSnapshotService().resolve_snapshot(
            session, context, UUID(state["knowledge_base_id"]), state["snapshot_id"]
        )
        if (
            snapshot.content_hash != state["snapshot_hash"]
            or snapshot.item_count != state["documents"]
        ):
            raise ValueError("snapshot persistence differs from prepared state")
    return context


def learned_index(configuration):
    return LearnedSparseIndex(
        url=configuration.qdrant_url, collection_name=COLLECTION, dense_vector_size=1024
    )


async def prepare(args, protocol: dict, state: dict) -> None:
    configuration = pinned_settings()
    engine, factory = create_database(configuration.database_url)
    components, index = None, None
    manifest_path = args.output / "prepared-index.json"
    if manifest_path.exists():
        raise ValueError("refusing to overwrite a prepared learned index manifest")
    try:
        context = await checked_context(factory, state)
        components = production_retrieval_components(configuration)
        sparse = LearnedM3SparseEncoder(
            dense_embedder=components.dense, model_directory=MODEL_DIRECTORY, batch_size=8
        )
        gate = await asyncio.to_thread(numerical_gate, components.dense, sparse)
        dump(args.output / "numerical-gate.json", gate)
        index = learned_index(configuration)
        await asyncio.to_thread(index.ensure_collection)
        # Only the explicitly named isolated new collection may be written.
        if index.collection_name == components.index.collection_name:
            raise ValueError("source collection cannot be the learned target")
        retriever = OptimizedRetriever(
            session_factory=factory,
            dense_embedder=components.dense,
            sparse_encoder=sparse,
            reranker=components.reranker,
            vector_index=index,
        )
        query = RetrievalQuery("scope", state["knowledge_base_id"], state["snapshot_id"])
        async with factory() as session:
            workspace_id, kb_id, revision_ids = await retriever._snapshot_scope(
                session, context, query
            )
        scope = VectorScope(str(workspace_id), str(kb_id), revision_ids)
        iterator = iter_dense_records(components.index, scope=scope, batch_size=128)
        seen, nnz = set(), []
        started = time.perf_counter()
        for page in iterator:
            async with factory() as session:
                chunks = await retriever._load_chunks(
                    session,
                    snapshot_id=UUID(state["snapshot_id"]),
                    workspace_id=workspace_id,
                    knowledge_base_id=kb_id,
                    revision_ids=revision_ids,
                    chunk_ids={record.chunk_id for record in page},
                )
            if len(chunks) != len(page):
                raise ValueError("source vectors include missing or out-of-snapshot database rows")
            if any(
                record.chunk_id in seen or record.chunk_id not in state["chunk_sources"]
                for record in page
            ):
                raise ValueError("source has duplicate or unexpected chunk membership")
            vectors = await asyncio.to_thread(
                sparse.encode_documents, [chunks[record.chunk_id][0].text for record in page]
            )
            records = tuple(
                VectorRecord(
                    record.point_id, record.chunk_id, record.dense, encoding, record.payload
                )
                for record, encoding in zip(page, vectors, strict=True)
            )
            await asyncio.to_thread(index.upsert, records)
            seen.update(record.chunk_id for record in page)
            nnz.extend(len(encoding.indices) for encoding in vectors)
            if len(seen) % 1024 == 0:
                print(f"learned indexed {len(seen)}/{state['chunks']}", flush=True)
        if seen != set(state["chunk_sources"]) or len(seen) != state["chunks"]:
            raise ValueError("learned index source does not cover every frozen chunk")
        dump(
            manifest_path,
            {
                "snapshot_id": state["snapshot_id"],
                "snapshot_hash": state["snapshot_hash"],
                "chunks": len(seen),
                "index_profile": protocol["arms"]["learned"],
                "model_revision": protocol["model_revision"],
                "numerical_gate": gate,
                "mean_sparse_nnz": sum(nnz) / len(nnz),
                "max_sparse_nnz": max(nnz),
                "indexing_seconds": time.perf_counter() - started,
                "dense_policy": "Existing dense vectors copied exactly; not re-encoded",
                "protocol": protocol,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
        print(f"learned index ready: {len(seen)} chunks", flush=True)
    finally:
        if index is not None:
            index.close()
        if components is not None:
            components.index.close()
        await engine.dispose()


async def run(args, protocol: dict, state: dict) -> None:
    manifest = json.loads((args.output / "prepared-index.json").read_text(encoding="utf-8"))
    if manifest["protocol"] != protocol or manifest["chunks"] != state["chunks"]:
        raise ValueError("learned index is not prepared for this protocol")
    if args.action == "regression":
        if not args.frozen:
            raise ValueError("regression requires frozen DEV selection")
        frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
        if frozen["protocol"] != protocol or frozen["selected_arm"] not in ARMS:
            raise ValueError("frozen selection differs from protocol")
    split = "train" if args.action == "dev" else "test"
    paths = {name: args.output / f"{args.action}-{name}.jsonl" for name in ARMS}
    if any(p.exists() or p.with_suffix(".meta.json").exists() for p in paths.values()):
        raise ValueError("refusing to overwrite a historical run")
    exposure(
        args.output,
        "LEARNED_DEV_LABEL_ACCESS"
        if split == "train"
        else "LEARNED_EXPOSED_TEST_REGRESSION_LABEL_ACCESS",
        protocol=str(args.protocol),
    )
    source = load_scifact(args.data, split)
    configuration = pinned_settings()
    engine, factory = create_database(configuration.database_url)
    components, index, handles = None, None, {}
    try:
        context = await checked_context(factory, state)
        components = production_retrieval_components(configuration)
        sparse = LearnedM3SparseEncoder(
            dense_embedder=components.dense, model_directory=MODEL_DIRECTORY, batch_size=8
        )
        index = learned_index(configuration)
        await asyncio.to_thread(index.ensure_collection)
        cold = {}
        for name, component in (
            ("dense", components.dense),
            ("tokenizer_sparse", components.sparse),
            ("learned_sparse", sparse),
            ("reranker", components.reranker),
        ):
            start = time.perf_counter()
            await asyncio.to_thread(component.warm)
            cold[name] = (time.perf_counter() - start) * 1000
        import torch

        retrievers = {
            name: OptimizedRetriever(
                session_factory=factory,
                dense_embedder=components.dense,
                sparse_encoder=components.sparse if name == "baseline" else sparse,
                reranker=components.reranker,
                vector_index=(
                    components.index
                    if name == "baseline"
                    else SharedDenseTreatmentIndex(components.index, index)
                ),
                rrf_k=60,
            )
            for name in ARMS
        }
        qids = sorted(source.queries)
        random.Random(protocol["seed"]).shuffle(qids)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        for name, path in paths.items():
            dump(
                path.with_suffix(".meta.json"),
                {
                    "schema_version": 4,
                    "arm": name,
                    "phase": args.action,
                    "split": split,
                    "queries": len(qids),
                    "seed": protocol["seed"],
                    "snapshot_id": state["snapshot_id"],
                    "common_controls": common_controls(),
                    "index_profile": protocol["arms"][name],
                    "model_revision": protocol["model_revision"],
                    "protocol": protocol,
                    "paired_interleaved": True,
                    "cold_load_ms": cold,
                    "git_commit": commit,
                    "git_dirty": True,
                    "device": torch.cuda.get_device_name(0),
                    "torch": torch.__version__,
                    "started_at": datetime.now(UTC).isoformat(),
                    "production_default_changed": False,
                },
            )
            handles[name] = path.open("x", encoding="utf-8")
        rng = random.Random(protocol["seed"] + 23)
        for ordinal, qid in enumerate(qids):
            order = list(ARMS)
            rng.shuffle(order)
            for name in order:
                retriever = retrievers[name]
                start = time.perf_counter()
                result = await retriever.retrieve_with_trace(
                    context,
                    RetrievalQuery(
                        source.queries[qid],
                        state["knowledge_base_id"],
                        state["snapshot_id"],
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
                row = {
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
                    "output_characters": sum(len(hit.text) for hit in result.evidence),
                }
                handles[name].write(json.dumps(row) + "\n")
                handles[name].flush()
            if ordinal == 0 or (ordinal + 1) % 25 == 0 or ordinal + 1 == len(qids):
                print(f"{args.action} paired {ordinal + 1}/{len(qids)}", flush=True)
    finally:
        for handle in handles.values():
            handle.close()
        if index is not None:
            index.close()
        if components is not None:
            components.index.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "dev", "regression"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--frozen", type=Path)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    validate_protocol(protocol, state)
    offline_models()
    asyncio.run(
        prepare(args, protocol, state) if args.action == "prepare" else run(args, protocol, state)
    )


if __name__ == "__main__":
    main()
