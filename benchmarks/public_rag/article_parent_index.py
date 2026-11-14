"""Build a pinned title-only companion to the exhaustive SciFact article index."""

import hashlib
import json
import os
import time
from pathlib import Path

from benchmarks.public_rag.reranker_batch_benchmark import PROTOCOL, checked_file, save, sha256
from benchmarks.public_rag.scifact_recall_index import (
    MODEL,
    MODEL_SHA256,
    frozen_source,
    normalize_chunk_text,
    save_vectors,
)
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder


def build(output):
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    source = frozen_source(json.loads(PROTOCOL.read_bytes()))
    for name in MODEL_SHA256:
        checked_file(MODEL, name, MODEL_SHA256)
    dense = BgeM3DenseEmbedder(model_name=str(MODEL), device="cuda", batch_size=8)
    sparse = BgeM3SparseEncoder(model_name=str(MODEL))
    texts = [normalize_chunk_text(d.title) for d in source.corpus.values()]
    start = time.perf_counter()
    save_vectors(output, "title-", texts, dense, sparse, len(sparse._load_tokenizer()))
    save(
        output / "title-index.json",
        {
            "documents": len(texts),
            "seconds": time.perf_counter() - start,
            "ordered_ids_sha256": hashlib.sha256(
                json.dumps(list(source.corpus)).encode()
            ).hexdigest(),
            "sha256": {
                p.name: sha256(p) for p in (output / "title-dense.npy", output / "title-sparse.npz")
            },
        },
    )


if __name__ == "__main__":
    import sys

    build(Path(sys.argv[1]))
