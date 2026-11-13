import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from benchmarks.evaluation.experiments.citation_lifecycle_dev import runner
from benchmarks.evaluation.experiments.citation_lifecycle_dev.runner import (
    load_cases,
    local_api,
    prepare,
    score,
)
from scripts.aliyun_api_smoke import SmokeFailure


def test_frozen_fixture_and_local_transport_scope():
    cases = load_cases()
    assert [c["case"] for c in cases] == list(range(1, 11))
    assert Counter(c["target"] for c in cases) == {"current": 6, "archive": 2, "abstain": 2}
    assert local_api("http://127.0.0.1:9870/api/v1") == "http://127.0.0.1:9870/api/v1/"
    with pytest.raises(SmokeFailure):
        local_api("https://example.test/api/v1")


def test_gold_counts_value_citation_and_explicit_abstention_together():
    cases = load_cases()
    refs = {"current": "r1", "archive": "r2"}

    def graded(case, text, ids, insufficient=False):
        return score(
            case,
            refs,
            {
                "answer": text,
                "insufficient_evidence": insufficient,
                "citations": [{"document_revision_id": i} for i in ids],
            },
        )["pass"]

    assert graded(cases[0], "17 hours [1]", ["r1"])
    assert not graded(cases[0], "117 hours [17]", ["r1"])
    assert not graded(cases[0], "17 hours [2]", ["r2"])
    assert graded(cases[6], "19 days [2]", ["r2"])
    assert not graded(cases[6], "19 days [2]", ["r2"], True)
    assert graded(cases[9], "资料未提供费用。", [], True)
    assert not graded(cases[9], "费用为66元。", [], False)
    assert not graded(cases[9], "未知 [1]", ["r1"], True)
    with pytest.raises(SmokeFailure):
        graded(cases[9], "unknown", [], "true")
    with pytest.raises(KeyError):
        score(cases[9], refs, {"answer": "unknown", "citations": []})


def test_preparation_uploads_only_text_and_supersedes_the_correct_revision():
    case = load_cases()[0]
    documents = [str(UUID(int=i)) for i in (1, 2)]
    revisions = [str(UUID(int=i)) for i in (10, 11)]
    uploaded = []

    def handle(req):
        path = req.url.path
        if path.endswith("/documents") and req.method == "POST":
            index = len(uploaded)
            uploaded.append(req.content)
            field = ("current_body", "archived_body")[index]
            assert case[field].encode() in req.content
            assert case["query"].encode() not in req.content and b'"target"' not in req.content
            data = {"document": {"id": documents[index]}, "revision": {"id": revisions[index]}}
        elif req.method == "PATCH":
            assert path.endswith("/documents/" + documents[1])
            assert json.loads(req.content) == {"superseded_by_document_id": documents[0]}
            data = {}
        elif path.endswith("/revisions"):
            index = documents.index(path.split("/")[-2])
            data = [{"revision": {"id": revisions[index], "ingestion_status": "READY"}}]
        else:
            assert req.method == "POST" and path.endswith("/snapshots")
            data = {"id": "snapshot", "item_count": 2}
        return httpx.Response(200, json=data)

    with httpx.Client(
        transport=httpx.MockTransport(handle), base_url="http://test/api/v1/"
    ) as client:
        snapshot, refs = prepare(client, "workspaces/w/knowledge-bases/k", [case])
    assert snapshot["item_count"] == 2 and len(uploaded) == 2
    assert refs == {1: {"current": revisions[0], "archive": revisions[1]}}


@pytest.mark.parametrize("lost_response", [False, True])
def test_public_path_sends_one_qa_per_case_and_retires_credential(monkeypatch, lost_response):
    cases = load_cases()[:2]
    ids = {
        k: str(UUID(int=i)) for i, k in enumerate(("org", "ws", "kb", "snap", "cred", "profile"), 1)
    }
    refs = {c["case"]: {"current": "cur", "archive": "old"} for c in cases}
    snapshot = {"id": ids["snap"], "item_count": 4, "content_hash": "a" * 64}
    monkeypatch.setattr(runner, "prepare", lambda client, kb, cases: (snapshot, refs))
    monkeypatch.setattr(runner, "register", lambda client: ("operator", {}))
    qa_requests, cleanup = [], []

    def handle(req):
        path = req.url.path
        if path.endswith("/ready"):
            data = {}
        elif path.endswith("/organizations"):
            data = {"id": ids["org"]}
        elif path.endswith("/workspaces"):
            data = {"id": ids["ws"]}
        elif path.endswith("/knowledge-bases"):
            data = {"id": ids["kb"]}
        elif path.endswith("/provider-credentials"):
            data = {"id": ids["cred"]}
        elif path.endswith("/model-profiles"):
            data = {"id": ids["profile"]}
        elif path.endswith("/retrieval/playground"):
            data = {
                "snapshot_id": ids["snap"],
                "total_latency_ms": 2,
                "evidence": [
                    {"document_revision_id": "cur", "superseded": False},
                    {"document_revision_id": "old", "superseded": True},
                ],
            }
        elif path.endswith("/knowledge/query"):
            body = json.loads(req.content)
            case = cases[len(qa_requests)]
            assert body == {
                "workspace_id": ids["ws"],
                "knowledge_base_id": ids["kb"],
                "knowledge_snapshot_id": ids["snap"],
                "model_profile_id": ids["profile"],
                "query": case["query"],
                "output_mode": "PROMPT_JSON",
            }
            qa_requests.append(case["case"])
            if lost_response:
                raise httpx.ReadTimeout("transport interrupted", request=req)
            data = {
                "answer": case["current"] + " [1]",
                "insufficient_evidence": False,
                "citations": [{"document_revision_id": "cur"}],
                "retrieval_trace": {"snapshot_id": ids["snap"]},
            }
        else:
            assert ids["cred"] in path
            cleanup.append((req.method, path))
            data = {}
        return httpx.Response(200, json=data)

    route = SimpleNamespace(
        provider="openai-compatible",
        model="synthetic",
        base_url="https://example.test",
        api_key="synthetic",
        max_tokens=512,
        timeout_seconds=60,
        capabilities={},
    )
    report = {"status": "INCOMPLETE", "cases": []}
    snapshots = []
    monkeypatch.setattr(runner, "checkpoint", lambda path, data: snapshots.append(json.dumps(data)))
    with httpx.Client(
        transport=httpx.MockTransport(handle), base_url="http://test/api/v1/"
    ) as client:
        if lost_response:
            with pytest.raises(httpx.ReadTimeout):
                runner.exercise(client, route, cases, Path("result.json"), report)
            assert qa_requests == [1]
            assert report["cases"][-1]["state"] == "QA_REQUEST_STARTED"
        else:
            runner.exercise(client, route, cases, Path("result.json"), report)
            assert qa_requests == [1, 2]
            assert report["status"] == "PASS"
    assert json.loads(snapshots[-1]) == report
    assert [m for m, _ in cleanup] == ["PATCH", "POST"]
    assert report["cleanup"]["rotate"]["confirmed"]
