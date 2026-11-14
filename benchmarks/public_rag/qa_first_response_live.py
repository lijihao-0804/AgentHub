"""Capture once-only live Citation QA results against isolated, pre-provisioned snapshots."""

import hashlib
import json
import time
from datetime import UTC, datetime

from benchmarks.public_rag.article_live_capture import exclusive, write


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def summarize(events, mapping):
    def ref(chunk):
        item = mapping[chunk]
        return {"source": item["source_id"], "ordinal": item["ordinal"], "chunk_id": chunk}

    for event in events:
        for k in ("candidate", "reranked", "chunks"):
            if k in event:
                event[k] = [ref(chunk) for chunk in event[k]]
    return events


def capture(
    client, *, state, dataset, cases, mapping, arm, protocol, trace_file, destination, source
):
    identity = dict(
        arm=arm,
        source=source,
        model=state["model"],
        profile=state["profile"],
        snapshot=dataset["snapshot"],
        snapshot_hash=dataset["snapshot_sha256"],
        documents=dataset["count"],
        chunks=len(mapping),
        protocol_sha256=sha(protocol.read_text(encoding="utf8")),
        endpoint="POST /api/v1/knowledge/query",
    )
    with exclusive(destination):
        if destination.exists():
            raise RuntimeError("First responses already dispatched for this cohort and arm")
        report = dict(**identity, rows=[])
        for case in cases:
            offset = trace_file.stat().st_size if trace_file.exists() else 0
            payload = dict(
                workspace_id=dataset["workspace"],
                knowledge_base_id=dataset["kb"],
                knowledge_snapshot_id=dataset["snapshot"],
                model_profile_id=state["profile"],
                output_mode="PROMPT_JSON",
                query=case["query"],
            )
            if arm == "candidate":
                payload["request_mode"] = "non_thinking"
            row = dict(id=case["id"], state="dispatched", utc=datetime.now(UTC).isoformat())
            report["rows"].append(row)
            write(destination, report)
            start = time.perf_counter()
            try:
                reply = client.post("knowledge/query", json=payload)
            except Exception as exc:
                row.update(state="unknown_transport", error=type(exc).__name__)
                write(destination, report)
                raise
            row.update(
                state="returned",
                http=reply.status_code,
                elapsed_s=round(time.perf_counter() - start, 3),
            )
            if reply.is_success:
                body = reply.json()
                assert body["retrieval_trace"]["snapshot_id"] == dataset["snapshot"]
                row.update(
                    answer=body["answer"], insufficient=body["insufficient_evidence"], citations=[]
                )
                for c in body["citations"]:
                    item = mapping[c["chunk_id"]]
                    text = item["text"]
                    excerpt = c["excerpt"]
                    assert (
                        c["document_revision_id"] == item["revision"]
                        and excerpt in text
                        and excerpt
                    )
                    i = text.index(excerpt)
                    row["citations"].append(
                        dict(
                            number=c["id"],
                            source=item["source_id"],
                            ordinal=item["ordinal"],
                            chunk_id=c["chunk_id"],
                            revision=item["revision"],
                            excerpt=excerpt,
                            chunk_text_sha256=sha(text),
                            excerpt_sha256=sha(excerpt),
                            excerpt_start_in_chunk=i,
                            excerpt_end_in_chunk=i + len(excerpt),
                            locator=c["locator"],
                        )
                    )
            else:
                err = reply.json()["error"]
                row.update(error=err["code"], message=err["message"])
            if trace_file.exists():
                with trace_file.open("rb") as f:
                    f.seek(offset)
                    row["events"] = summarize([json.loads(line) for line in f], mapping)
            write(destination, report)
