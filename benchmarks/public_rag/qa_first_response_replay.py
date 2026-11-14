"""Rebuild public QA snapshots and compare two local source trees using a real loopback API."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import httpx

from benchmarks.public_rag.article_live_capture import write
from benchmarks.public_rag.candidate_recall_live_replay import (
    CORPUS_SHA256,
    chunk_sources,
    local_api,
    prepare,
    provision_workspace,
)
from benchmarks.public_rag.qa_first_response_cases import OUT, ROOT, project
from benchmarks.public_rag.qa_first_response_live import capture
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from scripts.aliyun_api_smoke import (
    aliyun_route,
    create_model_profile,
    create_provider_credential,
)
from scripts.support_api_smoke import authenticate, register, retire_credential

SOURCE_FILES = (
    "packages/knowledge/citation_qa.py",
    "apps/api/routes/citation_qa.py",
    "apps/api/schemas/citation_qa.py",
    "packages/model_gateway/adapters/litellm/adapter.py",
    "packages/model_gateway/gateway.py",
    "packages/model_gateway/contracts.py",
    "packages/model_gateway/errors.py",
    "packages/knowledge/retrieval.py",
)


@contextmanager
def server(source, directory, name, collection=None):
    trace = directory / f"{name}-events.jsonl"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [
        sys.executable,
        "-c",
        "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
        "from benchmarks.public_rag.qa_first_response_trace import install;"
        "install(Path(sys.argv[2]));from apps.api.main import app;"
        "from packages.knowledge import citation_qa;"
        "assert Path(citation_qa.__file__).resolve().is_relative_to(Path(sys.argv[1]));"
        "import uvicorn;uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[3]),"
        "loop='none',access_log=False)",
        str(source.resolve()),
        str(trace.resolve()),
        str(port),
    ]
    with (directory / f"{name}-server.log").open("xb") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env={
                **os.environ,
                **({"AGENTHUB_KNOWLEDGE_QDRANT_COLLECTION": collection} if collection else {}),
            },
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}/api/v1/", timeout=240, trust_env=False
            ) as client:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Benchmark API exited during startup")
                    try:
                        if client.get("ready").is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("Benchmark API readiness timeout")
                yield client, trace
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def load_documents(scope, cases, papers):
    if scope == "full":
        corpus = ROOT / "data/external/rag-public-20261021/scifact/extracted/scifact/corpus.jsonl"
        if hashlib.sha256(corpus.read_bytes()).hexdigest() != CORPUS_SHA256:
            raise ValueError("SciFact corpus differs from the frozen source")
        with corpus.open(encoding="utf8") as stream:
            return {
                r["_id"]: r["title"].strip() + "\n\n" + r["text"].strip()
                for line in stream
                if (r := json.loads(line))
            }
    if scope == "qasper":
        historical = json.loads((OUT / "protocol.json").read_bytes())["cases"]
        cases = [c for c in historical if c["scope"] == "qasper"]
    return {c["gold_documents"][0]: papers[c["gold_documents"][0]] for c in cases}


def map_chunks(client, path, snapshot, revisions, docs):
    http = chunk_sources(client, path, snapshot, revisions)
    result = {}
    for revision, key in revisions.items():
        parsed = ParsedDocument((ParsedBlock(docs[key], {"type": "text_range"}),))
        for c in build_deterministic_chunks(
            UUID(revision), parsed, chunk_size_chars=1200, chunk_overlap_chars=200
        ):
            assert http[c.chunk_id] == key
            result[c.chunk_id] = dict(
                source_id=key, ordinal=c.ordinal, revision=revision, text=c.text
            )
    assert result.keys() == http.keys()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url", required=True, help="Isolated API with worker and migrated DB."
    )
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument(
        "--budget-pairs",
        action="store_true",
        help="Run paired 512/2048 validation on the chosen frozen budget cohort.",
    )
    parser.add_argument(
        "--reader-triplets", action="store_true", help="Replay the three frozen reader arms"
    )
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--scopes",
        nargs="+",
        choices=("independent", "reserve14", "reserve16", "qasper", "full"),
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--full-api-url",
        help="Loopback API for SciFact, sharing the same database and authentication settings.",
    )
    parser.add_argument("--full-collection", help="Name of that isolated Qdrant collection.")
    args = parser.parse_args()
    if args.reader_triplets and args.budget_pairs:
        parser.error("Select one protocol")
    alternate = args.budget_pairs or args.reader_triplets
    default_scopes = None
    if args.reader_triplets:
        from benchmarks.public_rag.qa_first_response_reader import capture_reader_scope
        from scripts.citation_qa_profile import create_qa_profile

        protocol = OUT.parent / "reader/protocol3.json"
        valid = ("reserve16", "reserve14", "qasper", "full")
        default_scopes = ["reserve16"]
        if not args.prepare_only and not (args.baseline_root and args.candidate_root):
            parser.error("Provide the original and strict reader source roots")
    elif args.budget_pairs:
        default_scopes = ["reserve14"]
        args.candidate_root = args.candidate_root or ROOT
        from benchmarks.public_rag.qa_first_response_budget import assemble, capture_budget_pair
        from scripts.citation_qa_profile import create_qa_profile

        protocol = OUT.parent / "budget/protocol2.json"
        valid = ("reserve14", "qasper", "full")
    else:
        protocol = OUT / "protocol.json"
        valid = ("independent", "qasper", "full")
        if not args.prepare_only and not (args.baseline_root and args.candidate_root):
            parser.error("Provide both frozen source roots for the request-mode experiment")
    args.scopes = args.scopes or default_scopes or list(valid)
    if not set(args.scopes) <= set(valid):
        parser.error("Scope is not part of the selected protocol")
    if alternate and len(args.scopes) > 1:
        parser.error(
            "Run each validation scope with its own isolated collection and output directory"
        )
    if (
        "full" in args.scopes
        and len(args.scopes) > 1
        and not (args.full_api_url and args.full_collection)
    ):
        parser.error(
            "Mixed-corpus replay requires --full-api-url and --full-collection "
            "to preserve collection-level sparse IDF."
        )
    if bool(args.full_api_url) != bool(args.full_collection):
        parser.error("Provide both full-collection options together")
    if len(set(args.scopes)) != len(args.scopes):
        parser.error("Each scope may be selected only once")
    if not args.prepare_only:
        for source in (
            (args.candidate_root,)
            if args.budget_pairs
            else (args.baseline_root, args.candidate_root)
        ):
            if not all((source / name).is_file() for name in SOURCE_FILES):
                parser.error(f"Incomplete application source tree: {source}")
    args.output.mkdir(parents=True, exist_ok=False)
    all_cases, papers = project(protocol)
    with httpx.Client(base_url=local_api(args.api_url), timeout=240, trust_env=False) as setup:
        _, owner = register(setup)
        provisioned = {}
        credentials = []
        try:
            for scope in args.scopes:
                cases = [c for c in all_cases if c["scope"] == scope]
                docs = load_documents(scope, cases, papers)
                if args.reader_triplets and scope == "reserve14":
                    budget_cases, budget_papers = project(OUT.parent / "budget/protocol2.json")
                    docs = load_documents(
                        scope, [c for c in budget_cases if c["scope"] == scope], budget_papers
                    )
                if alternate and scope == "qasper":
                    original_cases, original_papers = project()
                    independent_cases = [c for c in original_cases if c["scope"] == "independent"]
                    _, _, idf_path = provision_workspace(setup, "QASPER IDF companion")
                    companion_docs = load_documents(
                        "independent", independent_cases, original_papers
                    )
                    idf_snapshot, _ = prepare(setup, idf_path, companion_docs)
                    write(args.output / "idf-companion-snapshot.json", idf_snapshot)
                ws, kb, path = provision_workspace(setup, scope + " first-response QA")
                if scope == "full" and args.full_api_url:
                    with httpx.Client(
                        base_url=local_api(args.full_api_url), timeout=240, trust_env=False
                    ) as full_client:
                        authenticate(full_client, owner)
                        snapshot, revisions = prepare(full_client, path, docs)
                        mapping = map_chunks(full_client, path, snapshot["id"], revisions, docs)
                else:
                    snapshot, revisions = prepare(setup, path, docs)
                    mapping = map_chunks(setup, path, snapshot["id"], revisions, docs)
                ds = dict(
                    workspace=ws,
                    kb=kb,
                    snapshot=snapshot["id"],
                    snapshot_sha256=snapshot["content_hash"],
                    count=len(docs),
                )
                write(args.output / f"{scope}-snapshot.json", ds)
                if args.prepare_only:
                    continue
                route = aliyun_route()
                collection = f"workspaces/{ws}"
                credentials.append([collection + "/provider-credentials", None])
                credential = create_provider_credential(setup, collection, route)
                credentials[-1][1] = credential
                if alternate:
                    if (
                        route.max_tokens,
                        float(route.timeout_seconds),
                        float(route.temperature),
                    ) != (2048, 60, 0):
                        raise ValueError("Configured route differs from the budget protocol")
                    profiles = {
                        str(n): create_qa_profile(
                            setup, collection, route, credential, output_allowance=n
                        )
                        for n in (512, 2048)
                    }
                    if args.reader_triplets:
                        capture_reader_scope(
                            directory=args.output,
                            public=args.output,
                            protocol=protocol,
                            scope=scope,
                            sources={"original": args.baseline_root, "strict": args.candidate_root},
                            collection=args.full_collection if scope == "full" else None,
                            owner=owner,
                            dataset=ds,
                            mapping=mapping,
                            profiles=profiles,
                            model=route.model,
                        )
                        continue
                    source = args.candidate_root
                    hashes = {
                        f: hashlib.sha256((source / f).read_bytes()).hexdigest()
                        for f in SOURCE_FILES
                    }
                    with server(
                        source,
                        args.output,
                        "budget-" + scope,
                        args.full_collection if scope == "full" else None,
                    ) as (client, trace):
                        authenticate(client, owner)
                        for case in cases:
                            capture_budget_pair(
                                client,
                                case=case,
                                profiles=profiles,
                                model=route.model,
                                dataset=ds,
                                mapping=mapping,
                                protocol=protocol,
                                trace_file=trace,
                                directory=args.output,
                                source=hashes,
                            )
                    assemble(args.output, args.output, protocol, scopes=[scope])
                    continue
                profile = create_model_profile(setup, collection, route, credential)
                provisioned[scope] = (dict(model=route.model, profile=profile), ds, cases, mapping)
            if args.prepare_only or alternate:
                return
            for arm, source in (
                ("baseline", args.baseline_root),
                ("candidate", args.candidate_root),
            ):
                hashes = {
                    f: hashlib.sha256((source / f).read_bytes()).hexdigest() for f in SOURCE_FILES
                }
                for scope, (state, ds, cases, mapping) in provisioned.items():
                    name = f"{arm}-{scope}"
                    with server(
                        source, args.output, name, args.full_collection if scope == "full" else None
                    ) as (client, trace):
                        authenticate(client, owner)
                        capture(
                            client,
                            state=state,
                            dataset=ds,
                            cases=cases,
                            mapping=mapping,
                            arm=arm,
                            protocol=OUT / "protocol.json",
                            trace_file=trace,
                            destination=args.output / f"{name}.json",
                            source=hashes,
                        )
        finally:
            receipts = [
                retire_credential(setup, owner, path, credential, "Smoke")
                for path, credential in credentials
            ]
            write(args.output / "credential-retirement.json", receipts)
            if any(
                not all(r.get(k, {}).get("confirmed") for k in ("rotate", "disable"))
                for r in receipts
            ):
                raise RuntimeError("Credential retirement requires operator reconciliation")


if __name__ == "__main__":
    main()
