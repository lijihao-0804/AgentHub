"""Measure the pinned 8B Q4 encoder on the smaller shared laptop cohort via loopback."""

import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import numpy as np

from .compare import digest, laptop_sample, save

GGUF_SHA = "3fcd3febec8b3fd64435204db75bf0dd73b91e8d0661e0331acfe7e7c3120b85"
PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
)


def measure(client, data):
    eot = (
        client.post("/tokenize", json={"content": "<|endoftext|>", "parse_special": True})
        .raise_for_status()
        .json()["tokens"]
    )
    if len(eot) != 1:
        raise ValueError("Expected Qwen end-of-text token")

    def encode(text):
        tokens = (
            client.post("/tokenize", json={"content": text, "add_special": False})
            .raise_for_status()
            .json()["tokens"]
        )
        ids = tokens[:511] + eot
        response = client.post("/v1/embeddings", json={"input": ids}).raise_for_status().json()
        vector = np.asarray(response["data"][0]["embedding"], dtype=np.float32)
        if vector.shape != (4096,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise ValueError("Expected one finite 4096-dimensional vector")
        return vector / np.linalg.norm(vector)

    doc_ids, documents = zip(*data["corpus"].items(), strict=True)
    start = time.perf_counter()
    corpus = np.stack([encode(document) for document in documents])
    index_seconds = time.perf_counter() - start
    start = time.perf_counter()
    queries = np.stack([encode(PREFIX + text) for _, text, _ in data["questions"]])
    query_seconds = time.perf_counter() - start
    ranks = np.argsort(-(queries @ corpus.T), axis=1, kind="stable")
    per_query = {}
    for rank, (qid, _, gold) in zip(ranks, data["questions"], strict=True):
        per_query[qid] = {
            k: len(set(gold) & {doc_ids[i] for i in rank[:k]}) / len(gold) for k in (1, 5, 20)
        }
    return {
        "model": "Qwen/Qwen3-Embedding-8B-GGUF",
        "quantization": "Q4_K_M",
        "gguf_sha256": GGUF_SHA,
        "runtime": "llama.cpp b7550",
        "pool_sha256": digest(json.dumps(data, ensure_ascii=False).encode()),
        "queries": len(queries),
        "documents": len(documents),
        "max_model_tokens": 512,
        "dimension": 4096,
        "index_seconds": round(index_seconds, 3),
        "query_seconds": round(query_seconds, 3),
        "macro_recall": {
            k: sum(v[k] for v in per_query.values()) / len(per_query) for k in (1, 5, 20)
        },
        "per_question": per_query,
    }


def run(args):
    import hashlib

    with args.weights.open("rb") as weights:
        if hashlib.file_digest(weights, "sha256").hexdigest() != GGUF_SHA:
            raise ValueError("Expected official pinned GGUF")
    with socket.socket() as check:
        check.bind(("127.0.0.1", args.port))
    cmd = [
        str(args.binary),
        "-m",
        str(args.weights),
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--embedding",
        "--pooling",
        "last",
        "-ngl",
        str(args.gpu_layers),
        "-c",
        "1024",
        "-np",
        "1",
        "-b",
        "512",
        "-ub",
        "512",
        "-t",
        "4",
    ]
    data = laptop_sample()
    started = time.perf_counter()
    with (
        args.log.open("xb") as log,
        httpx.Client(
            base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=90
        ) as client,
    ):
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            while time.perf_counter() - started < 180:
                if proc.poll() is not None:
                    raise RuntimeError(f"Local embedding server exited with {proc.returncode}")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("Local embedding server did not start")
            load_seconds = time.perf_counter() - started
            results = measure(client, data)
            save(
                args.output,
                {
                    **results,
                    "load_seconds": round(load_seconds, 3),
                    "gpu_layers_requested": args.gpu_layers,
                },
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=30)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    for arg in ("binary", "weights", "log", "output"):
        cli.add_argument("--" + arg, type=Path, required=True)
    cli.add_argument("--port", type=int, default=56612)
    cli.add_argument("--gpu-layers", type=int, default=16)
    run(cli.parse_args())
