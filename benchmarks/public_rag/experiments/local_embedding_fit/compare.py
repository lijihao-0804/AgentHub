"""Compare local dense encoders in a fixed, deliberately confusable FiQA document pool."""

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path

from benchmarks.public_rag.candidate_recall_methods import WordBM25

DATA = Path("data/external/rag-fiqa-20261022/extracted/fiqa")
FIXTURE = Path(".scratch/composite-20261118/embedding-pool.json")
MODELS = {
    "bge": ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"),
    "mini": (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
    ),
    "qwen": ("Qwen/Qwen3-Embedding-0.6B", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


def load_rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def prepare():
    gold = {}
    with (DATA / "qrels/dev.tsv").open(encoding="utf8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            if int(row["score"]) > 0:
                gold.setdefault(row["query-id"], []).append(row["corpus-id"])
    selected = sorted(gold, key=lambda q: digest(("local-embedding-fit-v1/" + q).encode()))[:80]
    queries = {r["_id"]: r["text"] for r in load_rows(DATA / "queries.jsonl")}
    corpus = {r["_id"]: r["text"] for r in load_rows(DATA / "corpus.jsonl")}
    index = WordBM25({k: (k, v) for k, v in corpus.items()})
    pool = set()
    for q in selected:
        pool.update(gold[q])
        pool.update(hit["document_id"] for hit in index.query(queries[q], limit=12))
    save(
        FIXTURE,
        {
            "corpus": {k: corpus[k] for k in sorted(pool)},
            "questions": [[q, queries[q], sorted(gold[q])] for q in selected],
        },
    )


def laptop_sample():
    data = json.loads(FIXTURE.read_bytes())
    questions = data["questions"][:12]
    index = WordBM25({key: (key, value) for key, value in data["corpus"].items()})
    ids = {doc for _, _, gold in questions for doc in gold}
    for _, question, _ in questions:
        ids.update(hit["document_id"] for hit in index.query(question, limit=5))
    return {"corpus": {k: data["corpus"][k] for k in sorted(ids)}, "questions": questions}


def measure(name, model_dir, output, small=False):
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    model_id, revision = MODELS[name]
    if model_dir.name != revision:
        raise ValueError(f"Expected a pinned {model_id} snapshot at {revision}")
    data = laptop_sample() if small else json.loads(FIXTURE.read_bytes())
    ids, docs = zip(*data["corpus"].items(), strict=True)
    questions = data["questions"]
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    model = SentenceTransformer(
        str(model_dir),
        device="cuda",
        model_kwargs={"torch_dtype": torch.float16},
        local_files_only=True,
    )
    model.max_seq_length = min(model.max_seq_length, 512)
    torch.cuda.synchronize()
    load_s = time.perf_counter() - started
    kwargs = {"batch_size": 4, "normalize_embeddings": True, "show_progress_bar": False}
    started = time.perf_counter()
    documents = model.encode(list(docs), **kwargs)
    torch.cuda.synchronize()
    index_s = time.perf_counter() - started
    started = time.perf_counter()
    query_kwargs = {"prompt_name": "query"} if name == "qwen" else {}
    vectors = model.encode([q[1] for q in questions], **kwargs, **query_kwargs)
    torch.cuda.synchronize()
    query_s = time.perf_counter() - started
    scores = vectors.astype("float32") @ documents.astype("float32").T
    if not np.isfinite(scores).all():
        raise ValueError("An encoder produced a non-finite score")
    ranks = np.argsort(-scores, axis=1, kind="stable")
    per_question = {}
    for ranked, (qid, _, gold) in zip(ranks, questions, strict=True):
        per_question[qid] = {
            k: len(set(gold) & {ids[i] for i in ranked[:k]}) / len(gold) for k in (1, 5, 20)
        }
    save(
        output,
        {
            "model": model_id,
            "revision": revision,
            "pool_sha256": (
                digest(json.dumps(data, ensure_ascii=False).encode())
                if small
                else digest(FIXTURE.read_bytes())
            ),
            "queries": len(questions),
            "documents": len(ids),
            "gpu": torch.cuda.get_device_name(),
            "torch": str(torch.__version__),
            "dimension": vectors.shape[1],
            "max_model_tokens": model.max_seq_length,
            "query_prefix": model.prompts.get("query") if name == "qwen" else None,
            "load_seconds": round(load_s, 3),
            "index_seconds": round(index_s, 3),
            "query_batch_seconds": round(query_s, 3),
            "cuda_peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1),
            "macro_recall": {
                k: sum(r[k] for r in per_question.values()) / len(questions) for k in (1, 5, 20)
            },
            "per_question": per_question,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", *MODELS))
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--small", action="store_true", help="Use the shared 8B feasibility cohort."
    )
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.model_dir and args.output:
        measure(args.action, args.model_dir, args.output, args.small)
    else:
        parser.error("Each model run needs --model-dir and --output.")
