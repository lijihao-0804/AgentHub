"""Rebuild the two public QASPER libraries and replay retrieval or real QA on loopback."""

import argparse
import hashlib
from pathlib import Path
from uuid import UUID

import httpx

from benchmarks.public_rag.article_live_capture import write
from benchmarks.public_rag.candidate_recall_live_replay import local_api, prepare
from benchmarks.public_rag.evidence_retention_live import run_scope
from benchmarks.public_rag.qa_first_response_budget import assemble
from benchmarks.public_rag.qa_first_response_cases import ROOT, TRAIN, paper_text, read
from benchmarks.public_rag.qa_first_response_replay import map_chunks
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from scripts.aliyun_api_smoke import aliyun_route, create_provider_credential, request
from scripts.citation_qa_profile import create_qa_profile
from scripts.support_api_smoke import authenticate, register, retire_credential

DEFAULT = ROOT / "benchmarks/public_rag/results/published/evidence-retention-20261114"
SCOPES = ("independent", "historical")


def upload(client, owner, state, scope, paper_ids, papers, directory):
    authenticate(client, owner)
    ws_path = f"workspaces/{state['workspace']}"
    kb = request(client, "POST", ws_path + "/knowledge-bases", {"name": "QASPER " + scope})["id"]
    base = f"{ws_path}/knowledge-bases/{kb}"
    document_ids = {}

    def record_upload(response):
        if (
            response.request.method == "POST"
            and response.request.url.path.endswith("/documents")
            and response.is_success
        ):
            response.read()
            result = response.json()
            document_ids[result["revision"]["id"]] = result["document"]["id"]

    client.event_hooks["response"].append(record_upload)
    docs = {key: paper_text(papers[key]) for key in paper_ids}
    snapshot, revisions = prepare(client, base, docs)
    client.event_hooks["response"].remove(record_upload)
    authenticate(client, owner)
    chunks = map_chunks(client, base, snapshot["id"], revisions, docs)
    for revision, key in revisions.items():
        parsed = ParsedDocument((ParsedBlock(docs[key], {"type": "text_range"}),))
        for chunk in build_deterministic_chunks(
            UUID(revision), parsed, chunk_size_chars=1200, chunk_overlap_chars=200
        ):
            chunks[chunk.chunk_id].update(document_id=document_ids[revision], locator=chunk.locator)
    state["datasets"][scope] = {
        "workspace": state["workspace"],
        "kb": kb,
        "snapshot": snapshot["id"],
        "snapshot_sha256": snapshot["content_hash"],
        "count": len(docs),
        "chunks": len(chunks),
    }
    write(directory / f"{scope}-chunks.json", chunks)
    write(directory / "state.json", state)


def provision(args):
    protocol = read(args.protocol)
    if hashlib.sha256(TRAIN.read_bytes()).hexdigest() != protocol["train_sha256"]:
        raise ValueError("Incorrect QASPER TRAIN file")
    papers = read(TRAIN)
    args.state_dir.mkdir(parents=True, exist_ok=False)
    route = aliyun_route()
    if (route.max_tokens, float(route.temperature), float(route.timeout_seconds)) != (2048, 0, 60):
        raise ValueError("Configured QA route differs from the protocol")
    with (
        httpx.Client(
            base_url=local_api(args.independent_api_url), timeout=300, trust_env=False
        ) as independent,
        httpx.Client(
            base_url=local_api(args.historical_api_url), timeout=300, trust_env=False
        ) as historical,
    ):
        _, owner = register(independent)
        write(args.state_dir / "private-login.json", owner)
        org = request(independent, "POST", "organizations", {"name": "Evidence retention replay"})
        workspace = request(
            independent,
            "POST",
            "workspaces",
            {"name": "QASPER DEV replay", "organization_id": org["id"]},
        )
        ws = "workspaces/" + workspace["id"]
        state = {"workspace": workspace["id"], "datasets": {}, "credential": None}
        write(args.state_dir / "state.json", state)
        state["credential"] = create_provider_credential(independent, ws, route)
        write(args.state_dir / "state.json", state)
        state.update(
            profile=create_qa_profile(independent, ws, route, state["credential"]),
            model=route.model,
        )
        write(args.state_dir / "state.json", state)
        for scope, client in zip(SCOPES, (independent, historical), strict=True):
            upload(client, owner, state, scope, protocol["corpora"][scope], papers, args.state_dir)


def run(args):
    state = read(args.state_dir / "state.json")
    frozen = read(args.freeze)
    if hashlib.sha256(args.protocol.read_bytes()).hexdigest() != frozen["protocol_sha256"]:
        raise ValueError("Protocol differs from frozen execution")
    for name, expected in frozen["source_sha256_lf"].items():
        if (
            hashlib.sha256((ROOT / name).read_text(encoding="utf-8").encode()).hexdigest()
            != expected
        ):
            raise ValueError(f"Source file differs from frozen execution: {name}")
    source = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in frozen["source"]
    }
    args.output.mkdir(parents=True, exist_ok=False)
    with httpx.Client(base_url=local_api(args.api_url), timeout=300, trust_env=False) as client:
        run_scope(
            client,
            scope=args.scope,
            phase=args.phase,
            protocol=args.protocol,
            mapping=read(args.state_dir / f"{args.scope}-chunks.json"),
            dataset=state["datasets"][args.scope],
            state=state,
            source=source,
            trace=args.events,
            directory=args.output,
            owner=read(args.state_dir / "private-login.json"),
        )
    assemble(args.output, args.output, args.protocol, scopes=[args.scope], arms=("A", "B"))


def retire(args):
    state = read(args.state_dir / "state.json")
    owner = read(args.state_dir / "private-login.json")
    with httpx.Client(base_url=local_api(args.api_url), timeout=300, trust_env=False) as client:
        authenticate(client, owner)
        receipt = retire_credential(
            client,
            owner,
            f"workspaces/{state['workspace']}/provider-credentials",
            state["credential"],
            "Smoke",
        )
    write(args.state_dir / "retirement.json", receipt)
    if receipt.get("matching_credentials") != 0 and not all(
        receipt.get(k, {}).get("confirmed") for k in ("disable", "rotate")
    ):
        raise RuntimeError("Local benchmark credential retirement requires reconciliation")
    (args.state_dir / "private-login.json").unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    setup = sub.add_parser("prepare", help="Upload and snapshot the 48 and 16 public papers")
    setup.add_argument("--independent-api-url", required=True)
    setup.add_argument("--historical-api-url", required=True)
    setup.add_argument("--state-dir", required=True, type=Path)
    setup.add_argument("--protocol", type=Path, default=DEFAULT / "protocol.json")
    for phase in ("retrieval", "live", "retire"):
        p = sub.add_parser(phase)
        p.add_argument("--api-url", required=True)
        p.add_argument("--state-dir", required=True, type=Path)
        if phase != "retire":
            p.add_argument("--scope", choices=SCOPES, required=True)
            p.add_argument("--events", required=True, type=Path)
            p.add_argument("--output", required=True, type=Path)
            p.add_argument("--protocol", type=Path, default=DEFAULT / "protocol.json")
            p.add_argument("--freeze", type=Path, default=DEFAULT / "execution-freeze.json")
    args = parser.parse_args()
    if args.phase == "prepare":
        provision(args)
    elif args.phase == "retire":
        retire(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
