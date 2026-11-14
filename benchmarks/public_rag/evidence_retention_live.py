"""Capture model-free retrieval and once-only first responses using the instrumented local API."""

import hashlib
import json
import time
from datetime import UTC, datetime

from benchmarks.public_rag.article_live_capture import exclusive, write
from benchmarks.public_rag.qa_first_response_cases import read
from benchmarks.public_rag.qa_first_response_live import capture, summarize
from scripts.support_api_smoke import authenticate

PROFILE = {"dense_top_k": 30, "sparse_top_k": 30, "candidate_top_k": 20, "final_top_k": 6}
PLAYGROUND = "POST /api/v1/workspaces/{workspace_id}/knowledge-bases/{kb_id}/retrieval/playground"


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def identity(state, dataset, mapping, arm, protocol, source, phase):
    return {
        "arm": arm,
        "source": source,
        "model": state["model"],
        "profile": state["profile"],
        "snapshot": dataset["snapshot"],
        "snapshot_hash": dataset["snapshot_sha256"],
        "documents": dataset["count"],
        "chunks": len(mapping),
        "protocol_sha256": digest_bytes(protocol.read_bytes()),
        "endpoint": "POST /api/v1/knowledge/query" if phase == "live" else PLAYGROUND,
        "knowledge_base": dataset["kb"],
        "workspace": dataset["workspace"],
        "request_mode": "provider_default",
        "output_mode": "PROMPT_JSON",
        "retrieval_profile": PROFILE,
    }


def join_events(events, mapping):
    rendering = [e for e in events if e["kind"] == "reader_entries"]
    readers = [e for e in events if e["kind"] == "reader"]
    if len(rendering) != len(readers):
        raise ValueError("Unmatched reader measurement")
    for reader, detail in zip(readers, rendering, strict=True):
        reader["entries"] = detail["entries"]
        if reader["prompt_sha256"] != detail["prompt_sha256"] or [
            e["chunk_id"] for e in reader["entries"]
        ] != [chunk["chunk_id"] for chunk in reader["chunks"]]:
            raise ValueError("Reader capture differs from rendered input")
        for item in reader["entries"]:
            record = mapping[item["chunk_id"]]
            if not (
                item["text"] == record["text"]
                and item["document_revision_id"] == record["revision"]
                and item["document_id"] == record["document_id"]
                and item["locator"] == record["locator"]
            ):
                raise ValueError("Visible input is inconsistent with the published snapshot")
    return [e for e in events if e["kind"] != "reader_entries"]


def bind_response(response, row, mapping):
    response.read()
    row["response_sha256"] = digest_bytes(response.content)
    row["request_payload_sha256"] = digest_bytes(response.request.content)
    row["request_id"] = response.headers.get("x-request-id")
    if not response.is_success:
        return
    body = response.json()
    if "citations" not in body:
        return
    values = {item["id"]: item for item in body["citations"]}
    for citation in row["citations"]:
        value = values[citation["number"]]
        expected = mapping[citation["chunk_id"]]
        if (
            value["document_id"] != expected["document_id"]
            or value["locator"] != expected["locator"]
        ):
            raise ValueError("The returned citation locator or document differs from its source")
        citation.update(document_id=value["document_id"], citation_source=value["source"])


def capture_one(
    client,
    *,
    case,
    arm,
    state,
    dataset,
    mapping,
    protocol,
    source,
    trace,
    directory,
    owner,
    phase,
    retrieval_arm=None,
):
    if phase not in ("retrieval", "live") or arm not in ("A", "B"):
        raise ValueError("Unrecognized phase or arm")
    destination = directory / f"{arm}-{case['id']}.json"
    with exclusive(destination):
        if destination.exists():
            raise ValueError("This first request already has a receipt")
        client.headers["x-evidence-retention-arm"] = retrieval_arm or arm
        authenticate(client, owner)
        common = identity(state, dataset, mapping, arm, protocol, source, phase)
        if phase == "retrieval":
            offset = trace.stat().st_size if trace.exists() else 0
            when = datetime.now(UTC).isoformat()
            started = time.perf_counter()
            endpoint = (
                f"workspaces/{dataset['workspace']}/knowledge-bases/{dataset['kb']}"
                "/retrieval/playground"
            )
            response = client.post(
                endpoint,
                json={
                    "query": case["query"],
                    "knowledge_snapshot_id": dataset["snapshot"],
                    **PROFILE,
                },
            )
            if not response.is_success:
                raise RuntimeError(
                    f"Retrieval-only HTTP {response.status_code}: {response.text[:500]}"
                )
            if response.json()["snapshot_id"] != dataset["snapshot"]:
                raise ValueError("The retrieval-only endpoint used a different snapshot")
            with trace.open("rb") as stream:
                stream.seek(offset)
                events = summarize([json.loads(line) for line in stream], mapping)
            row = {
                "id": case["id"],
                "state": "returned",
                "utc": when,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "http": response.status_code,
                "events": join_events(events, mapping),
            }
        else:
            # A persistent dispatch receipt is written before entering the HTTP client.
            # If a process dies, capture() refuses to dispatch the same case and arm again.
            dispatched = directory / "dispatch" / destination.name
            seen = []

            def remember(response):
                if response.request.url.path.endswith("/knowledge/query"):
                    seen.append(response)

            client.event_hooks["response"].append(remember)
            try:
                capture(
                    client,
                    state=state,
                    dataset=dataset,
                    cases=[case],
                    mapping=mapping,
                    arm=arm,
                    protocol=protocol,
                    trace_file=trace,
                    destination=dispatched,
                    source=source,
                )
            finally:
                client.event_hooks["response"].remove(remember)
            (response,) = seen
            (row,) = read(dispatched)["rows"]
            row["events"] = join_events(row["events"], mapping)
        bind_response(response, row, mapping)
        row["physical_model_requests"] = sum(e["kind"] == "http_wire" for e in row["events"])
        write(destination, {**common, "rows": [row]})
        return row


def run_scope(
    client, *, scope, phase, protocol, mapping, dataset, state, source, trace, directory, owner
):
    cases = [case for case in read(protocol)["cases"] if case["scope"] == scope]
    if not cases:
        raise ValueError("Frozen cohort is empty")
    directory.mkdir(parents=True, exist_ok=True)
    if phase == "live":
        (directory / "dispatch").mkdir(exist_ok=True)
    for case in cases:
        arms = [
            {"baseline": "A", "candidate": "B", "A": "A", "B": "B"}[a] for a in case["arm_order"]
        ]
        if sorted(arms) != ["A", "B"]:
            raise ValueError("Expected one baseline and one candidate")
        for arm in arms:
            capture_one(
                client,
                case=case,
                arm=arm,
                state=state,
                dataset=dataset,
                mapping=mapping,
                protocol=protocol,
                source=source,
                trace=trace,
                directory=directory,
                owner=owner,
                phase=phase,
            )
