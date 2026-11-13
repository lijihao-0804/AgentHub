"""Recheck lifecycle-sensitive citation QA through a dedicated running local API."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_DNS, uuid5

import httpx

from scripts.aliyun_api_smoke import (
    MODEL,
    SmokeFailure,
    aliyun_route,
    check_url,
    create_model_profile,
    create_provider_credential,
    identifier,
    request,
    require,
)
from scripts.support_api_smoke import register, retire_credential

CASES = Path(__file__).with_name("cases.json")
DATA_SHA256_LF = "f42ed539784ff44fef4686ca0c1527c81a4ba78658079c417660ae589b43dc8f"


def load_cases() -> list[dict]:
    content = CASES.read_bytes().replace(b"\r\n", b"\n")
    require(hashlib.sha256(content).hexdigest() == DATA_SHA256_LF, "fixture digest differs")
    return json.loads(content)


def local_api(value: str) -> str:
    check_url(value)
    url = urlsplit(value)
    require(
        url.hostname in {"127.0.0.1", "localhost", "::1"} and url.path.rstrip("/") == "/api/v1",
        "supply the dedicated loopback AgentHub API ending in /api/v1",
    )
    return value.rstrip("/") + "/"


def checkpoint(path: Path, report: dict) -> None:
    pending = path.with_suffix(path.suffix + ".partial")
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def score(case: dict, revisions: dict, response: dict) -> dict:
    answer = response["answer"]
    citations = {item["document_revision_id"] for item in response["citations"]}
    insufficient = response["insufficient_evidence"]
    require(type(insufficient) is bool, "missing a validated Boolean insufficient_evidence")
    target = case["target"]
    result = {
        "answer": answer,
        "cited_revisions": sorted(citations),
        "insufficient_evidence": insufficient,
        "cites_current": revisions["current"] in citations,
        "cites_archived": revisions["archive"] in citations,
        "citation_count": len(response["citations"]),
    }
    if target == "abstain":
        result["pass"] = insufficient and not citations
    else:
        value = case["current" if target == "current" else "archived"]
        body = re.sub(r"\[\d+\]", "", answer)
        has_value = re.search(rf"(?<!\d){re.escape(value)}(?!\d)", body) is not None
        result["pass"] = not insufficient and has_value and revisions[target] in citations
    return result


def prepare(client, kb: str, cases: list[dict]) -> tuple[dict, dict]:
    revisions = {}
    pending = {}
    for case in cases:
        documents = {}
        refs = {}
        for variant, field in (("current", "current_body"), ("archive", "archived_body")):
            filename = uuid5(NAMESPACE_DNS, str(case["case"]) + variant).hex[:12] + ".txt"
            response = client.post(
                kb + "/documents", files={"file": (filename, case[field].encode(), "text/plain")}
            )
            require(response.is_success, f"upload returned HTTP {response.status_code}")
            upload = response.json()
            doc = documents[variant] = identifier(upload["document"]["id"])
            revision = refs[variant] = identifier(upload["revision"]["id"])
            pending[doc] = revision
        request(
            client,
            "PATCH",
            f"{kb}/documents/{documents['archive']}",
            {"superseded_by_document_id": documents["current"]},
        )
        revisions[case["case"]] = refs
    deadline = time.monotonic() + 180
    while pending:
        for doc, revision in tuple(pending.items()):
            statuses = request(client, "GET", f"{kb}/documents/{doc}/revisions")
            entry = next(s["revision"] for s in statuses if s["revision"]["id"] == revision)
            status = entry["ingestion_status"]
            require(status != "FAILED", "synthetic document ingestion failed")
            if status == "READY":
                del pending[doc]
        if pending:
            require(time.monotonic() < deadline, "timed out waiting for document ingestion")
            time.sleep(1)
    snapshot = request(client, "POST", kb + "/snapshots")
    require(snapshot["item_count"] == len(cases) * 2, "snapshot document count differs")
    return snapshot, revisions


def exercise(client, route, cases, path: Path, report: dict) -> None:
    request(client, "GET", "ready")
    _, login = register(client)
    org = request(client, "POST", "organizations", {"name": "Synthetic lifecycle DEV"})
    ws = request(client, "POST", "workspaces", {"organization_id": org["id"], "name": "QA DEV"})
    workspace = f"workspaces/{identifier(ws['id'])}"
    parent = workspace + "/knowledge-bases"
    kb = request(client, "POST", parent, {"name": "Synthetic release references"})
    kb_path = parent + "/" + identifier(kb["id"])
    snapshot, refs = prepare(client, kb_path, cases)
    report.update(
        workspace_id=ws["id"],
        knowledge_base_id=kb["id"],
        snapshot_id=snapshot["id"],
        snapshot_sha256=snapshot["content_hash"],
        documents_ready=snapshot["item_count"],
    )
    checkpoint(path, report)

    credential = None
    try:
        credential = create_provider_credential(client, workspace, route)
        profile = create_model_profile(client, workspace, route, credential)
        for case in cases:
            payload = {"knowledge_snapshot_id": snapshot["id"], "query": case["query"]}
            row = {"case": case["case"], "target": case["target"], "state": "RETRIEVING"}
            report["cases"].append(row)
            checkpoint(path, report)
            retrieval = request(
                client, "POST", kb_path + "/retrieval/playground", {**payload, "final_top_k": 6}
            )
            require(retrieval["snapshot_id"] == snapshot["id"], "retrieval snapshot mismatch")
            hits = {h["document_revision_id"]: h["superseded"] for h in retrieval["evidence"]}
            reference = refs[case["case"]]
            row["fixture_revisions"] = reference
            row["playground_revisions"] = [
                {"revision": h["document_revision_id"], "superseded": h["superseded"]}
                for h in retrieval["evidence"]
            ]
            row["both_versions_with_flags"] = (
                hits.get(reference["current"]) is False and hits.get(reference["archive"]) is True
            )
            row["playground_ms"] = retrieval["total_latency_ms"]
            row["state"] = "QA_REQUEST_STARTED"
            checkpoint(path, report)
            started = time.perf_counter()
            response = request(
                client,
                "POST",
                "knowledge/query",
                {
                    **payload,
                    "workspace_id": ws["id"],
                    "knowledge_base_id": kb["id"],
                    "model_profile_id": profile,
                    "output_mode": "PROMPT_JSON",
                },
            )
            row["qa_http_ms"] = round((time.perf_counter() - started) * 1000, 3)
            require(
                response["retrieval_trace"]["snapshot_id"] == snapshot["id"],
                "answer snapshot mismatch",
            )
            row.update(score(case, reference, response), state="COMPLETED")
            checkpoint(path, report)
    finally:
        # The shared creator gives this credential the name Smoke in this fresh workspace.
        report["cleanup"] = retire_credential(
            client, login, workspace + "/provider-credentials", credential, "Smoke"
        )
        checkpoint(path, report)
    cleanup = report["cleanup"]
    require(
        all(cleanup.get(k, {}).get("confirmed") for k in ("disable", "rotate")),
        "temporary credential cleanup was not confirmed",
    )
    report["status"] = (
        "PASS"
        if len(report["cases"]) == len(cases)
        and all(row["pass"] and row["both_versions_with_flags"] for row in report["cases"])
        else "FAIL"
    )
    checkpoint(path, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "run"))
    parser.add_argument("--api-url", help="Existing dedicated loopback API ending in /api/v1")
    parser.add_argument("--output", type=Path, help="Fresh output JSON path for a public B run")
    args = parser.parse_args()
    cases = load_cases()
    if args.mode == "validate":
        print(
            json.dumps(
                {"cases": len(cases), "documents": 2 * len(cases), "sha256_lf": DATA_SHA256_LF}
            )
        )
        return 0
    if args.api_url is None or args.output is None:
        parser.error("run requires --api-url and --output")
    endpoint = local_api(args.api_url)
    route = aliyun_route()
    report = {
        "status": "INCOMPLETE",
        "scope": "public B only",
        "registered": len(cases),
        "fixture_sha256_lf": DATA_SHA256_LF,
        "model": MODEL,
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(exist_ok=False)
    checkpoint(args.output, report)
    try:
        with httpx.Client(
            base_url=endpoint, timeout=180, trust_env=False, follow_redirects=False
        ) as client:
            exercise(client, route, cases, args.output, report)
    except (httpx.HTTPError, SmokeFailure, KeyError, ValueError, StopIteration, TypeError) as error:
        # Remote error bodies/configuration and access tokens never enter the report.
        report["error"] = str(error) if isinstance(error, SmokeFailure) else type(error).__name__
        checkpoint(args.output, report)
    print(json.dumps({"status": report["status"], "output": str(args.output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
