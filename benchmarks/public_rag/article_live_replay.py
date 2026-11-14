"""Replay frozen QASPER TRAIN questions against an isolated AgentHub installation."""

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import httpx
import numpy as np

from benchmarks.public_rag.article_live_blind import PUBLIC, read
from benchmarks.public_rag.article_live_capture import capture, write
from benchmarks.public_rag.candidate_recall_live_replay import (
    chunk_sources,
    local_api,
    prepare,
    provision_workspace,
)
from benchmarks.public_rag.scifact_recall_index import save_vectors
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from scripts.aliyun_api_smoke import (
    aliyun_route,
    create_model_profile,
    create_provider_credential,
    request,
)
from scripts.support_api_smoke import authenticate, register, retire_credential


def project(train):
    protocol = PUBLIC / "protocol-public.json"
    cases = [r for r in read(protocol)["cases"] if r["scope"] == "qasper"]
    if (
        hashlib.sha256(train.read_bytes()).hexdigest()
        != read(PUBLIC / "provenance.json")["corpus_sha256"]["QASPER train"]
    ):
        raise ValueError("wrong QASPER TRAIN file")
    raw = read(train)
    documents, parents = {}, {}
    for case in cases:
        (paper_id,) = case["gold_documents"]
        paper = raw[paper_id]
        qa = next(x for x in paper["qas"] if x["question_id"] == case["id"])
        if case["query"] != f"In the paper “{paper['title']}”, {qa['question']}":
            raise ValueError("frozen QASPER question differs from original")
        answer = qa["answers"][0]["answer"]
        if case["negative"] != answer["unanswerable"]:
            raise ValueError("answerability annotation differs")
        assert answer["extractive_spans"] == case["answers"]
        assert [hashlib.sha256(s.encode()).hexdigest() for s in answer["evidence"]] == case[
            "evidence_sha256"
        ]
        case["evidence"] = answer["evidence"]
        parents[paper_id] = paper["title"] + "\n\n" + paper["abstract"]
        body = [
            t for s in paper["full_text"] for t in [s["section_name"] or "", *s["paragraphs"]] if t
        ]
        documents[paper_id] = "\n\n".join([paper["title"], paper["abstract"], *body])
    assert {k: hashlib.sha256(v.encode()).hexdigest() for k, v in documents.items()} == read(
        PUBLIC / "provenance.json"
    )["qasper_frozen_projected_papers"]
    return protocol, cases, documents, parents


def build_assets(directory, dataset, documents, parents, revisions, http_ids, model):
    mapping = {}
    for revision, key in revisions.items():
        text = documents[key]
        blocks = ParsedDocument((ParsedBlock(text, {"type": "text_range"}),))
        for c in build_deterministic_chunks(
            UUID(revision), blocks, chunk_size_chars=1200, chunk_overlap_chars=200
        ):
            assert http_ids[c.chunk_id] == key
            mapping[c.chunk_id] = {
                "revision": revision,
                "source_id": key,
                "ordinal": c.ordinal,
                "text": c.text,
            }
    assert mapping.keys() == http_ids.keys()
    dense = BgeM3DenseEmbedder(model_name=model, device="cpu")
    sparse = BgeM3SparseEncoder(model_name=model)
    parent_ids = sorted(parents)
    order = list(mapping)
    save_vectors(
        directory,
        "qasper-parent-",
        [parents[k] for k in parent_ids],
        dense,
        sparse,
        len(sparse._load_tokenizer()),
    )
    chunk_file = directory / "qasper-chunk-dense.npy"
    np.save(
        chunk_file,
        np.asarray(dense.embed_documents([mapping[c]["text"] for c in order]), dtype=np.float32),
    )
    write(
        directory / "qasper-assets.json",
        {
            **dataset,
            "chunks": mapping,
            "parent_ids": parent_ids,
            "parent_dense": str(directory / "qasper-parent-dense.npy"),
            "parent_sparse": str(directory / "qasper-parent-sparse.npz"),
            "chunk_dense": str(chunk_file),
            "chunk_order": order,
        },
    )
    return mapping


@contextmanager
def candidate(directory, arm):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "benchmarks.public_rag.article_live_server",
        "--directory",
        str(directory),
        "--scope",
        "qasper",
        "--arm",
        arm,
        "--port",
        str(port),
    ]
    with (directory / f"{arm}.log").open("wb") as log:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}/api/v1/", timeout=240, trust_env=False
            ) as client:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError("candidate API failed to start")
                    try:
                        if client.get("ready").is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("candidate API readiness timeout")
                yield client
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-url", required=True)
    p.add_argument("--train", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--model", default="BAAI/bge-m3")
    p.add_argument(
        "--arms",
        nargs="+",
        choices=["baseline", "article_local_passage", "full_parent4"],
        default=["baseline", "article_local_passage", "full_parent4"],
    )
    p.add_argument(
        "--prepare-only",
        action="store_true",
        help="Upload, snapshot and build assets without invoking a provider.",
    )
    args = p.parse_args()
    proto, cases, docs, parents = project(args.train)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ.update(
        AGENTHUB_KNOWLEDGE_EMBEDDING_DEVICE="cpu", AGENTHUB_KNOWLEDGE_RERANKER_DEVICE="cpu"
    )
    with httpx.Client(base_url=local_api(args.api_url), timeout=240, trust_env=False) as client:
        request(client, "GET", "ready")
        _, owner = register(client)
        ws, kb, path = provision_workspace(client, "Public QASPER TRAIN article QA")
        snap, revs = prepare(client, path, docs)
        ds = {
            "workspace": ws,
            "kb": kb,
            "snapshot": snap["id"],
            "snapshot_sha256": snap["content_hash"],
            "count": len(revs),
        }
        chunks = chunk_sources(client, path, snap["id"], revs)
        mapping = build_assets(out, ds, docs, parents, revs, chunks, args.model)
        if args.prepare_only:
            return
        route = aliyun_route()
        collection = f"workspaces/{ws}/provider-credentials"
        credential = None
        try:
            credential = create_provider_credential(client, f"workspaces/{ws}", route)
            profile = create_model_profile(client, f"workspaces/{ws}", route, credential)
            for arm in args.arms:
                with candidate(out, arm) as arm_client:
                    authenticate(arm_client, owner)
                    capture(
                        arm_client,
                        ds,
                        profile,
                        route.model,
                        arm,
                        cases,
                        mapping,
                        out / f"qasper-{arm}.json",
                        proto,
                    )
        finally:
            receipt = retire_credential(client, owner, collection, credential, "Smoke")
            write(out / "credential-retirement.json", receipt)
            if not all(receipt.get(k, {}).get("confirmed") for k in ("rotate", "disable")):
                raise RuntimeError("credential retirement incomplete")


if __name__ == "__main__":
    main()
