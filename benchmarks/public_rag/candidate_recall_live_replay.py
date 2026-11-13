"""Replay the exposed SciFact DEV subset and account-policy fixture via an isolated local API."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx

from benchmarks.evaluation.experiments.citation_lifecycle_dev.runner import checkpoint, local_api
from scripts.aliyun_api_smoke import (
    aliyun_route,
    create_model_profile,
    create_provider_credential,
    request,
    require,
)
from scripts.support_api_smoke import register, retire_credential

EVIDENCE = Path(__file__).parent / "results/published/candidate-recall-20261114/live"
CORPUS = Path("data/external/rag-public-20261021/scifact/extracted/scifact/corpus.jsonl")
CORPUS_SHA256 = "dec31c8182f3d744c7d2c09423756fd1d17cbef75808db13ba01cc0aab4d1ac6"
POLICY = "lexical_refill_v1"
CANARY = (
    "Lattice Vault memorandum ZR-618. The color code assigned to the authorization "
    "desk is LILAC-726."
)


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cohort(name: str, corpus: Path) -> tuple[dict, dict[str, str], list[dict]]:
    if name == "product":
        frozen = read(EVIDENCE / "product-fixture.json")
        return frozen, frozen["documents"], [frozen["case"]]
    frozen = read(EVIDENCE / "subset-protocol.json")
    require(
        hashlib.sha256(corpus.read_bytes()).hexdigest() == CORPUS_SHA256,
        "SciFact corpus SHA differs",
    )
    selected = set(frozen["subset_documents"])
    documents = {}
    with corpus.open(encoding="utf-8") as rows:
        for line in rows:
            row = json.loads(line)
            if row["_id"] in selected:
                documents[row["_id"]] = row["title"].strip() + "\n\n" + row["text"].strip()
    require(len(documents) == 42 and set(documents) == selected, "frozen subset members differ")
    return frozen, documents, frozen["cases"]


def provision_workspace(client: httpx.Client, name: str) -> tuple[str, str, str]:
    org = request(client, "POST", "organizations", {"name": f"Candidate recall DEV {name}"})
    ws = request(client, "POST", "workspaces", {"organization_id": org["id"], "name": "QA DEV"})
    kb_path = f"workspaces/{ws['id']}/knowledge-bases"
    kb = request(client, "POST", kb_path, {"name": name})
    return ws["id"], kb["id"], kb_path + "/" + kb["id"]


def prepare(
    client: httpx.Client, kb_path: str, documents: dict[str, str]
) -> tuple[dict, dict[str, str]]:
    pending = {}
    revisions = {}
    for source_id, text in documents.items():
        filename = (
            "tenant-memorandum.txt" if source_id == "foreign-only" else f"scifact-{source_id}.txt"
        )
        reply = client.post(
            kb_path + "/documents", files={"file": (filename, text.encode(), "text/plain")}
        )
        require(reply.is_success, f"public upload returned HTTP {reply.status_code}")
        body = reply.json()
        revision = body["revision"]["id"]
        revisions[revision] = source_id
        pending[body["document"]["id"]] = revision
    deadline = time.monotonic() + 900
    while pending:
        for document, desired in list(pending.items()):
            rows = request(client, "GET", f"{kb_path}/documents/{document}/revisions")
            revision = next(row["revision"] for row in rows if row["revision"]["id"] == desired)
            require(revision["ingestion_status"] != "FAILED", "public document ingestion failed")
            if revision["ingestion_status"] == "READY":
                del pending[document]
        if pending:
            require(time.monotonic() < deadline, "timed out waiting for public ingestion")
            time.sleep(2)
    snapshot = request(client, "POST", kb_path + "/snapshots")
    require(snapshot["item_count"] == len(documents), "snapshot member count differs")
    return snapshot, revisions


def chunk_sources(client, kb_path, snapshot, revisions):
    result = {}
    offset = 0
    while True:
        page = request(
            client, "GET", f"{kb_path}/snapshots/{snapshot}/chunks?limit=20&offset={offset}"
        )
        for item in page["items"]:
            result[item["chunk_id"]] = revisions[item["document_revision_id"]]
        offset += len(page["items"])
        if offset == page["total"]:
            require(len(result) == offset, "duplicate snapshot chunk")
            return result
        require(page["items"] and offset < page["total"], "inconsistent chunk pagination")


def assess(reply, snapshot, chunks, revisions):
    if not reply.is_success:
        return {"http": reply.status_code, "error": reply.json()["error"]["code"]}
    body = reply.json()
    trace = body["retrieval_trace"]
    require(trace["snapshot_id"] == snapshot, "query used a different snapshot")
    stages = {
        stage: {
            "metadata": value.get("metadata"),
            "sources": [chunks[hit["chunk_id"]] for hit in value["results"]],
        }
        for stage, value in trace["stages"].items()
        if value is not None
    }
    return {
        "http": reply.status_code,
        "answer": body["answer"],
        "insufficient_evidence": body["insufficient_evidence"],
        "citations": [
            {
                "number": cite["id"],
                "source": revisions[cite["document_revision_id"]],
                "locator": cite["locator"],
                "excerpt_sha256": hashlib.sha256(cite["excerpt"].encode()).hexdigest(),
            }
            for cite in body["citations"]
        ],
        "stages": stages,
    }


def exercise(args, documents, cases, report):
    route = None if args.retrieval_only else aliyun_route()
    arms = ["A", "B"] if args.arms == "both" else [args.arms]
    with httpx.Client(base_url=local_api(args.api_url), timeout=300, trust_env=False) as client:
        request(client, "GET", "ready")
        register(client)
        foreign_ws, foreign_kb, foreign_path = provision_workspace(
            client, "Isolated foreign canary"
        )
        foreign_snapshot, _ = prepare(client, foreign_path, {"foreign-only": CANARY})
        _, owner = register(client)
        workspace, kb, kb_path = provision_workspace(client, args.cohort)
        snapshot, revisions = prepare(client, kb_path, documents)
        chunks = chunk_sources(client, kb_path, snapshot["id"], revisions)
        report.update(
            snapshot_sha256=snapshot["content_hash"], documents=len(revisions), chunks=len(chunks)
        )
        if "B" in arms:
            built = request(client, "POST", f"{kb_path}/snapshots/{snapshot['id']}/lexical-index")
            require(
                built["status"] == "READY" and built["chunk_count"] == len(chunks),
                "BM25 index incomplete",
            )
        workspace_path = "workspaces/" + workspace
        credential = None
        try:
            common = {
                "workspace_id": workspace,
                "knowledge_base_id": kb,
                "knowledge_snapshot_id": snapshot["id"],
                "output_mode": "PROMPT_JSON",
            }
            if route is not None:
                credential = create_provider_credential(client, workspace_path, route)
                common["model_profile_id"] = create_model_profile(
                    client, workspace_path, route, credential
                )
                report["model"] = route.model
            for arm in arms:
                policy = {} if arm == "A" else {"candidate_policy": POLICY}
                for case in cases:
                    row = {"arm": arm, "case": case["id"], "dispatch": "STARTED"}
                    report["requests"].append(row)
                    checkpoint(args.output, report)
                    if args.retrieval_only:
                        payload = {
                            "query": case["query"],
                            "knowledge_snapshot_id": snapshot["id"],
                            **policy,
                        }
                        got = request(client, "POST", kb_path + "/retrieval/playground", payload)
                        row.update(
                            dispatch="RETURNED",
                            fused_sources=[
                                chunks[v["chunk_id"]] for v in got["stages"]["fused"]["results"]
                            ],
                            evidence_sources=[chunks[v["chunk_id"]] for v in got["evidence"]],
                            metadata=got["stages"]["fused"].get("metadata"),
                        )
                        checkpoint(args.output, report)
                        continue
                    answer = client.post(
                        "knowledge/query", json={**common, **policy, "query": case["query"]}
                    )
                    row.update(
                        assess(answer, snapshot["id"], chunks, revisions), dispatch="RETURNED"
                    )
                    checkpoint(args.output, report)
                if args.retrieval_only:
                    continue
                for target_ws, target_kb in ((foreign_ws, foreign_kb), (workspace, kb)):
                    query = {
                        **common,
                        **policy,
                        "workspace_id": target_ws,
                        "knowledge_base_id": target_kb,
                        "knowledge_snapshot_id": foreign_snapshot["id"],
                        "query": "What is the color code in ZR-618?",
                    }
                    result = client.post("knowledge/query", json=query)
                    content = result.json()
                    report["tenant_checks"].append(
                        {
                            "arm": arm,
                            "http": result.status_code,
                            "error": content.get("error", {}).get("code"),
                            "returned_answer": "answer" in content,
                            "canary_leaked": "LILAC-726" in result.text,
                        }
                    )
                    require(
                        result.status_code in (403, 404) and "answer" not in content,
                        "foreign scope disclosed an answer",
                    )
                checkpoint(args.output, report)
        finally:
            if route is not None:
                report["credential_cleanup"] = retire_credential(
                    client, owner, workspace_path + "/provider-credentials", credential, "Smoke"
                )
            checkpoint(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "run"))
    parser.add_argument("--cohort", choices=("subset", "product"), required=True)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--api-url", help="Dedicated loopback API ending in /api/v1")
    parser.add_argument("--arms", choices=("A", "B", "both"), default="A")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Run the public playground without an external model or credential.",
    )
    args = parser.parse_args()
    _, documents, cases = load_cohort(args.cohort, args.corpus)
    if args.action == "validate":
        print(json.dumps({"documents": len(documents), "cases": [c["id"] for c in cases]}))
        return
    require(args.output is not None and not args.output.exists(), "supply a fresh output file")
    require(bool(args.api_url), "supply an explicitly isolated local API")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "cohort": args.cohort,
        "state": "INCOMPLETE",
        "retrieval_only": args.retrieval_only,
        "requests": [],
        "tenant_checks": [],
    }
    checkpoint(args.output, report)
    exercise(args, documents, cases, report)
    report["state"] = "COMPLETE"
    checkpoint(args.output, report)


if __name__ == "__main__":
    main()
