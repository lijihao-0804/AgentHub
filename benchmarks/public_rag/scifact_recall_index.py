"""Reconstruct the frozen SciFact TRAIN corpus vectors on local hardware."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from uuid import UUID

import numpy as np
from scipy import sparse

from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.reranker_batch_benchmark import (
    PROTOCOL,
    ROOT,
    checked_file,
    records,
    save,
    sha256,
)
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks, normalize_chunk_text
from packages.knowledge.parser import ParsedBlock, ParsedDocument

MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
MODEL = ROOT.parent / "hf_cache/models--BAAI--bge-m3/snapshots" / MODEL_REVISION
STATE = ROOT / ".scratch/rag-opt-20261021/scifact-state.json"
STATE_SHA = "cf0c3807844384745d1ad4edaf4c54ffa47fa2c64bb1b8ff4bfbff6a05914709"
# Pin the actual inputs used by SentenceTransformer and its tokenizer. A local
# folder name alone cannot establish the identity of a copy of the snapshot.
MODEL_SHA256 = {
    "pytorch_model.bin": "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
    "config.json": "26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa",
    "config_sentence_transformers.json": (
        "1eef72430e7194a1e59680e635aed81ffa083f05668dbc5bb1c56c04c0999c38"
    ),
    "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
    "sentence_bert_config.json": "eb9b44b13c0f52a3b3685c3b1cbdea1ba8b04bea123b98f61610048940776eb1",
    "1_Pooling/config.json": "e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352",
    "tokenizer.json": "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
    "tokenizer_config.json": "a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4",
    "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
}


def frozen_source(protocol: dict):
    data = ROOT / protocol["data_directory"]
    for name in ("corpus.jsonl", "queries.jsonl", "qrels/train.tsv"):
        checked_file(data, name, protocol["input_sha256"])
    return load_scifact(data, "train")


def chunk_manifest(source, state_path: Path, protocol: dict) -> list[dict]:
    if sha256(state_path) != STATE_SHA:
        raise ValueError("historical index inventory digest mismatch")
    state = json.loads(state_path.read_bytes())
    for key in ("snapshot_id", "snapshot_hash"):
        if state[key] != protocol[key]:
            raise ValueError(f"historical index {key} mismatch")
    # The original prepare() wrote this mapping in source and chunk creation order.
    by_document = defaultdict(list)
    for chunk_id, document_id in state["chunk_sources"].items():
        by_document[document_id].append(chunk_id)
    if list(by_document) != list(source.corpus):
        raise ValueError("historical document creation order differs from the source")
    path = checked_file(
        ROOT / protocol["historical_directory"], "pair-token-audit.jsonl", protocol["input_sha256"]
    )
    revisions = {}
    for row in records(path):
        if row["arm"] == "parent":
            doc = row["source_document_id"]
            if revisions.setdefault(doc, row["pair_id"]) != row["pair_id"]:
                raise ValueError(f"contradictory parent revision for {doc}")

    rows = []
    for doc, record in source.corpus.items():
        revision = revisions.get(doc)
        parsed = ParsedDocument((ParsedBlock(record.retrieval_text, {"type": "text_range"}),))
        # A revision changes only the identifier. No invented revision leaves this procedure.
        chunks = build_deterministic_chunks(
            UUID(revision) if revision else UUID(int=0),
            parsed,
            chunk_size_chars=state["chunk_size_chars"],
            chunk_overlap_chars=state["chunk_overlap_chars"],
        )
        for chunk, chunk_id in zip(chunks, by_document[doc], strict=True):
            if revision and chunk.chunk_id != chunk_id:
                raise ValueError(f"chunk/revision/content ID mismatch for {doc}")
            rows.append(
                {
                    "document_id": doc,
                    "chunk_id": chunk_id,
                    "ordinal": chunk.ordinal,
                    "revision_id": revision,
                    "char_start": chunk.locator["char_start"],
                    "char_end": chunk.locator["char_end"],
                    "text": chunk.text,
                    "text_sha256": chunk.normalized_content_hash,
                }
            )
    if len(rows) != protocol["corpus_chunks"]:
        raise ValueError("chunk inventory differs from the frozen protocol")
    return rows


def vector_matrix(texts, dense, lexical, width: int):
    vectors = np.empty((len(texts), dense.dimension), dtype=np.float32)
    starts = [0]
    indices = []
    values = []
    # The original scifact_runner.prepare called _vector_records in 128-chunk slices.
    for offset in range(0, len(texts), 128):
        batch = texts[offset : offset + 128]
        vectors[offset : offset + len(batch)] = dense.embed_documents(batch)
        for encoded in lexical.encode_documents(batch):
            indices.extend(encoded.indices)
            values.extend(encoded.values)
            starts.append(len(indices))
    lexical_matrix = sparse.csr_matrix(
        (values, indices, starts), shape=(len(texts), width), dtype=np.float32
    )
    return vectors, lexical_matrix


def save_vectors(directory: Path, prefix: str, texts, dense, lexical, width: int):
    matrix, lexical_matrix = vector_matrix(texts, dense, lexical, width)
    np.save(directory / f"{prefix}dense.npy", matrix)
    sparse.save_npz(directory / f"{prefix}sparse.npz", lexical_matrix)


def run(output: Path, model_dir: Path, state: Path) -> None:
    import sentence_transformers
    import torch
    import transformers

    output.mkdir(parents=True, exist_ok=False)
    os.environ["HF_HUB_OFFLINE"] = os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    protocol = json.loads(PROTOCOL.read_bytes())
    source = frozen_source(protocol)
    rows = chunk_manifest(source, state, protocol)
    with (output / "manifest.jsonl").open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    dense = BgeM3DenseEmbedder(model_name=str(model_dir), device="cuda", batch_size=8)
    lexical = BgeM3SparseEncoder(model_name=str(model_dir))
    width = len(lexical._load_tokenizer())
    model_hashes = {name: sha256(model_dir / name) for name in MODEL_SHA256}
    if model_hashes != MODEL_SHA256:
        raise ValueError("embedding/tokenizer file digest differs from the measured snapshot")
    save_vectors(output, "", [row["text"] for row in rows], dense, lexical, width)
    save(
        output / "chunks-ready.json",
        {
            "chunks": len(rows),
            "documents": len(source.corpus),
            "model_revision": MODEL_REVISION,
            "model_files_sha256": model_hashes,
            "snapshot_hash": protocol["snapshot_hash"],
            "state_sha256": STATE_SHA,
            "versions": {
                "torch": torch.__version__,
                "transformers": transformers.__version__,
                "sentence_transformers": sentence_transformers.__version__,
                "gpu": torch.cuda.get_device_name(),
            },
            "sha256": {
                n: sha256(output / n) for n in ("manifest.jsonl", "dense.npy", "sparse.npz")
            },
        },
    )
    print(f"ready: {len(rows)} chunk vectors", flush=True)
    doc_ids = list(source.corpus)
    save(output / "document-ids.json", doc_ids)
    save_vectors(
        output,
        "parent-",
        [normalize_chunk_text(source.corpus[d].retrieval_text) for d in doc_ids],
        dense,
        lexical,
        width,
    )
    query_ids = list(source.queries)
    save(output / "train-query-ids.json", query_ids)
    # Production embeds each retrieval query as a single input.
    pairs = [dense.embed_query(source.queries[q]) for q in query_ids]
    np.save(output / "train-query-dense.npy", np.asarray(pairs, dtype=np.float32))
    extra = (
        "parent-dense.npy",
        "parent-sparse.npz",
        "document-ids.json",
        "train-query-ids.json",
        "train-query-dense.npy",
    )
    save(output / "complete.json", {"sha256": {n: sha256(output / n) for n in extra}})
    print(
        f"ready: {len(doc_ids)} full articles and {len(query_ids)} TRAIN query vectors", flush=True
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=MODEL)
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args()
    run(args.output.resolve(), args.model_dir.resolve(), args.state.resolve())
