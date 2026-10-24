"""P-A1 pipeline: contextual prefixes vs original chunks, paired retrieval gate.

Stages: sample (seeded 20%) -> prefix (gateway situate calls, resumable) ->
index (production BGE-M3 into isolated ctxp-* Qdrant collections) ->
eval (hybrid RRF retrieval, nDCG@10 / Recall@20) -> gate (paired bootstrap
against the pre-registered thresholds in results/contextual-prefix-20261024).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid5

import httpx

RUN = Path(__file__).parent / "results" / "contextual-prefix-20261024"
MODEL = "deepseek-v4.1-flash-1017"
SEED = 20261024
SAMPLE_FRACTION = 0.2
CHUNK_CHARS = 1_200
CHUNK_OVERLAP = 200
UUID_NS = UUID("b16e0f3a-1a55-4d7f-9f2a-a7c10000000e")
DENSE_K = SPARSE_K = 100
CONCURRENCY = 6

DATASETS = {
    "scifact": {
        "corpus": Path("data/external/rag-public-20261021/scifact/extracted/scifact/corpus.jsonl"),
        "queries": Path(
            "data/external/rag-public-20261021/scifact/extracted/scifact/queries.jsonl"
        ),
        "qrels": Path("data/external/rag-public-20261021/scifact/extracted/scifact/qrels/test.tsv"),
    },
    "fiqa": {
        "corpus": Path("data/external/rag-fiqa-20261022/extracted/fiqa/corpus.jsonl"),
        "queries": Path("data/external/rag-fiqa-20261022/extracted/fiqa/queries.jsonl"),
        "qrels": Path("data/external/rag-fiqa-20261022/extracted/fiqa/qrels/test.tsv"),
    },
}


def _gateway() -> tuple[str, str]:
    base_url = os.environ.get("AGENTHUB_RAG_BASE_URL", "").strip("\"'")
    api_key = os.environ.get("AGENTHUB_RAG_API_KEY", "").strip("\"'")
    if not base_url or not api_key:
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENTHUB_RAG_BASE_URL="):
                base_url = line.split("=", 1)[1].strip().strip("\"'")
            elif line.startswith("AGENTHUB_RAG_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip("\"'")
    return base_url, api_key


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _load_qrels(path: Path) -> dict[str, set[str]]:
    qrels: dict[str, set[str]] = defaultdict(set)
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        query_id, doc_id, _score = line.split("\t")
        qrels[query_id].add(doc_id)
    return qrels


def _windows(text: str, *, size: int, overlap: int) -> list[str]:
    step = size - overlap
    windows: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        piece = text[start:end].strip()
        if piece:
            windows.append(piece)
        if end >= len(text):
            break
        start += step
    return windows or ([text.strip()] if text.strip() else [])


# ---------------------------------------------------------------------------
# stage: sample
# ---------------------------------------------------------------------------


def stage_sample() -> None:
    for name, spec in DATASETS.items():
        corpus = _load_jsonl(spec["corpus"])
        ids = sorted(doc["_id"] for doc in corpus)
        rng = random.Random(SEED)
        keep = set(rng.sample(ids, math.floor(len(ids) * SAMPLE_FRACTION)))
        sampled = [doc for doc in corpus if doc["_id"] in keep]
        target = RUN / f"sample-{name}.jsonl"
        with target.open("x", encoding="utf-8") as out:
            for doc in sampled:
                title = (doc.get("title") or "").strip()
                body = doc.get("text", "").strip()
                text = (title + '\n\n' + body).strip()
                record = json.dumps(
                    {"doc_id": doc["_id"], "text": text}, ensure_ascii=False
                )
                out.write(record + "\n")
        print(f"{name}: sampled {len(sampled)}/{len(corpus)} docs -> {target.name}")


# ---------------------------------------------------------------------------
# stage: prefix
# ---------------------------------------------------------------------------


async def stage_prefix() -> None:
    from benchmarks.public_rag.contextual_prefix import situate_once

    base_url, api_key = _gateway()
    for name in DATASETS:
        sample = _load_jsonl(RUN / f"sample-{name}.jsonl")
        target = RUN / f"prefixes-{name}.jsonl"
        done: set[str] = set()
        if target.exists():
            for line in target.read_text(encoding="utf-8").splitlines():
                done.add(json.loads(line)["key"])
        ledger = {"requests": 0, "valid": 0, "unprefixed": 0, "usage": {}}

        async with httpx.AsyncClient(timeout=45.0) as client:
            lock = asyncio.Lock()
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(
                key: str,
                chunk_text: str,
                *,
                _sem=sem,
                _lock=lock,
                _target=target,
                _name=name,
                _ledger=ledger,
            ):
                sem, lock, target, name, ledger = _sem, _lock, _target, _name, _ledger
                async with sem:
                    result = await situate_once(
                        client, base_url=base_url, api_key=api_key, model=MODEL,
                        chunk_text=chunk_text,
                    )
                async with lock:
                    ledger["requests"] += 1
                    if result["status"] == "VALID":
                        ledger["valid"] += 1
                    else:
                        ledger["unprefixed"] += 1
                    usage = result.get("usage")
                    if isinstance(usage, dict):
                        for field, value in usage.items():
                            ledger["usage"][field] = ledger["usage"].get(field, 0) + value
                    with target.open("a", encoding="utf-8") as out:
                        out.write(
                            json.dumps(
                                {
                                    "key": key,
                                    "status": result["status"],
                                    "prefix": result.get("value"),
                                    "chunk_chars": len(chunk_text),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    if ledger["requests"] % 200 == 0:
                        done = ledger["requests"]
                        print(f"{name}: {done} calls ({ledger['valid']} valid)", flush=True)

            jobs = []
            with target.open("a", encoding="utf-8") as out:
                del out
            for doc in sample:
                windows = _windows(doc["text"], size=CHUNK_CHARS, overlap=CHUNK_OVERLAP)
                for ordinal, chunk in enumerate(windows):
                    key = f'{doc["doc_id"]}::{ordinal}'
                    if key in done:
                        continue
                    jobs.append(one(key, chunk))
            if jobs:
                await asyncio.gather(*jobs)
        (RUN / f"prefix-ledger-{name}.json").write_text(
            json.dumps(ledger, indent=2), encoding="utf-8"
        )
        print(f"{name}: prefix phase {ledger}")


# ---------------------------------------------------------------------------
# stage: index + eval
# ---------------------------------------------------------------------------


def _components():
    from packages.core.config.settings import Settings
    from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
    from packages.knowledge.adapters.sparse import BgeM3SparseEncoder

    settings = Settings()
    dense = BgeM3DenseEmbedder(
        model_name=settings.knowledge_embedding_model,
        device=settings.knowledge_embedding_device,
        batch_size=settings.knowledge_embedding_batch_size,
        expected_dimension=settings.knowledge_dense_vector_size,
    )
    sparse = BgeM3SparseEncoder(model_name=settings.knowledge_embedding_model)
    return settings, dense, sparse


def _collection(arm: str, dataset: str, settings):
    from packages.knowledge.adapters.qdrant import QdrantVectorIndex

    return QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=f"ctxp-{dataset}-{arm}",
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=120,
    )


def _flush_records(index, batch: list) -> None:
    """Upsert and clear the pending batch; module-level so no loop capture."""
    if batch:
        index.upsert(tuple(batch))
        batch.clear()


def stage_index(arm: str) -> None:
    from packages.knowledge.contracts import VectorRecord

    settings, dense, sparse = _components()
    for name in DATASETS:
        prefixes = {}
        for line in (RUN / f"prefixes-{name}.jsonl").read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("prefix"):
                prefixes[entry["key"]] = entry["prefix"]
        sample = _load_jsonl(RUN / f"sample-{name}.jsonl")
        index = _collection(arm, name, settings)
        index.ensure_collection()
        started = time.perf_counter()
        batch: list[VectorRecord] = []

        def flush(_index=index, _batch=batch) -> None:
            _flush_records(_index, _batch)

        total = 0
        for doc in sample:
            for ordinal, chunk in enumerate(
                _windows(doc["text"], size=CHUNK_CHARS, overlap=CHUNK_OVERLAP)
            ):
                key = f'{doc["doc_id"]}::{ordinal}'
                embedded = chunk
                if arm == "prefixed":
                    prefix = prefixes.get(key)
                    if prefix:
                        from benchmarks.public_rag.contextual_prefix import prefixed_text

                        embedded = prefixed_text(prefix, chunk)
                chunk_id = str(uuid5(UUID_NS, f"{name}:{key}"))
                dense_vector = dense.embed_documents((embedded,))[0]
                encoding = sparse.encode_documents((embedded,))[0]
                batch.append(
                    VectorRecord(
                        point_id=str(uuid5(UUID_NS, f"{name}:{arm}:{key}")),
                        chunk_id=chunk_id,
                        dense=dense_vector,
                        sparse=encoding,
                        payload={
                            "workspace_id": "ctxp-benchmark",
                            "knowledge_base_id": f"ctxp-{name}-{arm}",
                            "document_revision_id": "ctxp-frozen",
                            "chunk_id": chunk_id,
                            "doc_id": doc["doc_id"],
                            "key": key,
                        },
                    )
                )
                total += 1
                if len(batch) >= 64:
                    flush()
                    print(f"{name}/{arm}: indexed {total}", flush=True)
        flush()
        (RUN / f"index-{name}-{arm}.json").write_text(
            json.dumps(
                {
                    "collection": f"ctxp-{name}-{arm}",
                    "documents": len(sample),
                    "chunks": total,
                    "prefixed_chunks": len(prefixes),
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        index.close()
        print(f"{name}/{arm}: indexed {total} chunks")


def stage_eval() -> None:
    settings, dense, sparse = _components()
    from packages.knowledge.retrieval import fuse_reciprocal_rank

    per_dataset = {}
    for name, spec in DATASETS.items():
        sample_ids = {doc["doc_id"] for doc in _load_jsonl(RUN / f"sample-{name}.jsonl")}
        queries = {q["_id"]: q["text"] for q in _load_jsonl(spec["queries"])}
        qrels = _load_qrels(spec["qrels"])
        evaluable = {
            query_id: (queries[query_id], gold & sample_ids)
            for query_id, gold in qrels.items()
            if query_id in queries and (gold & sample_ids)
        }
        arms = {}
        for arm in ("baseline", "prefixed"):
            index = _collection(arm, name, settings)
            from packages.knowledge.contracts import VectorScope

            scope = VectorScope(
                workspace_id="ctxp-benchmark",
                knowledge_base_id=f"ctxp-{name}-{arm}",
                document_revision_ids=("ctxp-frozen",),
            )
            rows = {}
            for query_id, (text, gold) in evaluable.items():
                dense_vector = dense.embed_query(text)
                dense_hits = index.dense_search(dense_vector, scope=scope, limit=DENSE_K)
                sparse_encoding = sparse.encode_query(text)
                sparse_hits = index.sparse_search(sparse_encoding, scope=scope, limit=SPARSE_K)
                fused = fuse_reciprocal_rank(
                    dense_hits, sparse_hits, rrf_k=60, candidate_top_k=DENSE_K
                )
                ranked_docs: list[str] = []
                seen: set[str] = set()
                for candidate in fused:
                    doc_id = None
                    for hit in dense_hits + sparse_hits:
                        if hit.payload.get("chunk_id") == candidate.chunk_id:
                            doc_id = hit.payload.get("doc_id")
                            break
                    if doc_id and doc_id not in seen:
                        seen.add(doc_id)
                        ranked_docs.append(doc_id)
                rows[query_id] = _ndcg_recall(ranked_docs, gold)
            index.close()
            arms[arm] = rows
        per_dataset[name] = {
            "evaluable_queries": len(evaluable),
            "arms": arms,
        }
        print(f"{name}: evaluated {len(evaluable)} queries x 2 arms")
    (RUN / "eval.json").write_text(json.dumps(per_dataset), encoding="utf-8")


def _ndcg_recall(ranked: list[str], gold: set[str], *, k: int = 10) -> dict[str, float]:
    relevance = [1.0 if doc in gold else 0.0 for doc in ranked[:k]]
    dcg = sum(rel / math.log2(position + 2) for position, rel in enumerate(relevance))
    ideal = sum(1.0 / math.log2(position + 2) for position in range(min(len(gold), k)))
    ndcg = dcg / ideal if ideal else 0.0
    hits = sum(relevance)
    recall20 = (
        sum(1.0 if doc in gold else 0.0 for doc in ranked[:20]) / len(gold)
    )
    return {"ndcg10": ndcg, "recall20": recall20, "hits10": hits}


def stage_gate() -> None:
    evaluation = json.loads((RUN / "eval.json").read_text(encoding="utf-8"))
    output = {}
    for name, block in evaluation.items():
        baseline, prefixed = block["arms"]["baseline"], block["arms"]["prefixed"]
        shared = sorted(set(baseline) & set(prefixed))
        deltas = {
            metric: [prefixed[q][metric] - baseline[q][metric] for q in shared]
            for metric in ("ndcg10", "recall20")
        }
        rng = random.Random(SEED)
        boot = {}
        for metric, values in deltas.items():
            samples = []
            for _ in range(5_000):
                draw = [values[rng.randrange(len(values))] for _ in values]
                samples.append(sum(draw) / len(draw))
            samples.sort()
            boot[metric] = {
                "delta_pp": round(sum(values) / len(values) * 100, 2),
                "ci95_pp": [
                    round(samples[int(0.025 * len(samples))] * 100, 2),
                    round(samples[int(0.975 * len(samples)) - 1] * 100, 2),
                ],
            }
        output[name] = {
            "n": len(shared),
            "baseline_ndcg10": round(
                sum(baseline[q]["ndcg10"] for q in shared) / len(shared), 4
            ),
            "prefixed_ndcg10": round(
                sum(prefixed[q]["ndcg10"] for q in shared) / len(shared), 4
            ),
            **boot,
        }
    both_positive = all(
        row["ndcg10"]["delta_pp"] >= 1.5 and row["ndcg10"]["ci95_pp"][0] > 0
        for row in output.values()
    )
    output["verdict"] = "PASS" if both_positive else "FAIL"
    (RUN / "gate.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("sample", "prefix", "index", "eval", "gate"))
    parser.add_argument("--arm", choices=("baseline", "prefixed"))
    args = parser.parse_args()
    if args.stage == "sample":
        stage_sample()
    elif args.stage == "prefix":
        asyncio.run(stage_prefix())
    elif args.stage == "index":
        if not args.arm:
            raise SystemExit("index requires --arm")
        stage_index(args.arm)
    elif args.stage == "eval":
        stage_eval()
    else:
        stage_gate()


if __name__ == "__main__":
    main()
