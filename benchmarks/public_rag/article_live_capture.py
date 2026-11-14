"""Capture a single paid HTTP result per case and arm with snapshot-verifiable citation text."""

import hashlib
import json
import os
import time
from contextlib import contextmanager


def write(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    tmp.replace(path)


@contextmanager
def exclusive(path):
    lock = path.with_suffix(".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        yield
    finally:
        os.close(descriptor)
        lock.unlink()


def capture(client, dataset, profile, model, arm, cases, mapping, path, protocol):
    with exclusive(path):
        _capture(client, dataset, profile, model, arm, cases, mapping, path, protocol)


def _capture(client, dataset, profile, model, arm, cases, mapping, path, protocol):
    def digest(value):
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    definition = {
        "arm": arm,
        "model": model,
        "model_profile": profile,
        "workspace": dataset["workspace"],
        "knowledge_base": dataset["kb"],
        "snapshot": dataset["snapshot"],
        "snapshot_hash": dataset["snapshot_sha256"],
        "documents": dataset["count"],
        "chunks": len(mapping),
        "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
        "source_mapping_sha256": digest(mapping),
        "ordered_cases_sha256": digest([(c["id"], c["query"]) for c in cases]),
        "endpoint": "POST /api/v1/knowledge/query",
    }
    report = json.loads(path.read_bytes()) if path.exists() else {**definition, "rows": []}
    if any(report.get(k) != v for k, v in definition.items()):
        raise ValueError("existing capture has a different or incomplete run identity")
    ids = [c["id"] for c in cases]
    completed = [r["id"] for r in report["rows"]]
    if len(ids) != len(set(ids)) or completed != ids[: len(completed)]:
        raise ValueError("stored case identities do not match the ordered cohort")
    if any(r["state"] != "returned" for r in report["rows"]):
        raise RuntimeError("previous ambiguous dispatch")
    for row in report["rows"]:
        if not isinstance(row.get("http"), int) or not (100 <= row["http"] < 600):
            raise ValueError("stored response missing HTTP status")
        for citation in row.get("citations", []):
            item = mapping[citation["chunk_id"]]
            if (
                item["revision"] != citation["revision"]
                or item["source_id"] != citation["source"]
                or item["ordinal"] != citation["ordinal"]
                or citation["excerpt"] not in item["text"]
            ):
                raise ValueError("stored citation is inconsistent with frozen chunk")
    for case in cases:
        if case["id"] in completed:
            continue
        payload = {
            "workspace_id": dataset["workspace"],
            "knowledge_base_id": dataset["kb"],
            "knowledge_snapshot_id": dataset["snapshot"],
            "model_profile_id": profile,
            "query": case["query"],
            "output_mode": "PROMPT_JSON",
        }
        row = {"id": case["id"], "state": "pending"}
        report["rows"].append(row)
        write(path, report)
        start = time.perf_counter()
        response = client.post("knowledge/query", json=payload)
        row.update(
            http=response.status_code,
            elapsed_s=round(time.perf_counter() - start, 3),
            state="returned",
        )
        if not response.is_success:
            row["error"] = response.json().get("error", {}).get("code")
        else:
            b = response.json()
            assert b["retrieval_trace"]["snapshot_id"] == dataset["snapshot"]
            row.update(answer=b["answer"], insufficient=b["insufficient_evidence"], citations=[])
            for c in b["citations"]:
                h = mapping[c["chunk_id"]]
                assert c["document_revision_id"] == h["revision"] and c["excerpt"] in h["text"]
                row["citations"].append(
                    dict(
                        number=c["id"],
                        source=h["source_id"],
                        ordinal=h["ordinal"],
                        chunk_id=c["chunk_id"],
                        revision=c["document_revision_id"],
                        excerpt=c["excerpt"],
                        locator=c["locator"],
                    )
                )
            row["stages"] = {
                s: [
                    {
                        "source": mapping[h["chunk_id"]]["source_id"],
                        "ordinal": mapping[h["chunk_id"]]["ordinal"],
                        "chunk_id": h["chunk_id"],
                    }
                    for h in v["results"]
                ]
                for s, v in b["retrieval_trace"]["stages"].items()
                if v is not None
            }
        write(path, report)
