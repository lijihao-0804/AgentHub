"""Reconstruct changed pre-normalization public answers and summarize measured cloud calls."""

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median

from benchmarks.public_rag.article_live_capture import write
from benchmarks.public_rag.qa_first_response_cases import read
from benchmarks.public_rag.qa_first_response_verify import events_of, require
from packages.knowledge.citation_qa import _CitationQaModelOutput, _validate_model_output
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS

PRIOR = Path(__file__).resolve().parent / "results/published/evidence-retention-20261114"


def pre_normalization(row, *, scope, arm):
    if row["http"] != 200:
        return None
    output = events_of(row, "model_output")[-1]
    require(output["json_status"] == "schema_valid", "Accepted raw output is unavailable")
    rendered = events_of(row, "reader")[0]
    raw = _CitationQaModelOutput.model_validate(output["output"], strict=True)
    ids = _validate_model_output(raw, evidence_count=len(rendered["entries"]))
    if (
        raw.answer == row["answer"]
        and raw.insufficient_evidence == row["insufficient"]
        and ids == tuple(c["number"] for c in row["citations"])
    ):
        return None
    require(arm == "B", "The original reader changed a terminal answer")
    citations = []
    for n in ids:
        chunk, entry = rendered["chunks"][n - 1], rendered["entries"][n - 1]
        require(chunk["chunk_id"] == entry["chunk_id"] and n == entry["number"], "Reader ID")
        citations.append(
            {
                "number": n,
                "source": chunk["source"],
                "ordinal": chunk["ordinal"],
                "chunk_id": chunk["chunk_id"],
                "revision": entry["document_revision_id"],
                "document_id": entry["document_id"],
                "locator": entry["locator"],
                "excerpt": entry["text"][:MAX_EVIDENCE_EXCERPT_CHARS],
            }
        )
    return {
        "id": row["id"],
        "scope": scope,
        "arm": arm,
        "http": row["http"],
        "answer": raw.answer,
        "insufficient": raw.insufficient_evidence,
        "citations": citations,
        "raw_model_content_sha256": output["content_sha256"],
    }


def operational(rows):
    events = [e for row in rows for e in row["events"]]
    usage = [e["usage"] for e in events if e["kind"] == "response" and e.get("usage")]
    times = sorted(row["elapsed_s"] for row in rows)
    physical = sum(row["physical_model_requests"] for row in rows)
    require(physical == sum(e["kind"] == "http_wire" for e in events), "Physical POST count")
    return {
        "first_http": len(rows),
        "http_status": dict(Counter(str(row["http"]) for row in rows)),
        "first_http_errors": [
            {"id": row["id"], "http": row["http"], "code": row["error"]}
            for row in rows
            if row["http"] != 200
        ],
        "physical_model_posts": physical,
        "provider_http_status": dict(
            Counter(str(e["status"]) for e in events if e["kind"] == "http_return")
        ),
        "provider_transport_error": dict(
            Counter(e["error_type"] for e in events if e["kind"] == "http_transport_error")
        ),
        "internal_correction_questions": [
            row["id"] for row in rows if row["physical_model_requests"] > 1
        ],
        "model_responses_with_usage": len(usage),
        "observed_tokens": {
            name: sum(u[name] for u in usage) for name in ("input_tokens", "output_tokens")
        },
        "elapsed_seconds_median": round(median(times), 3),
        "elapsed_seconds_p95": times[(95 * len(times) + 99) // 100 - 1],
    }


def historical_evidence(case, row, previous):
    prior_scope = "independent" if case["scope"] == "historical_vietnam" else "historical"
    old = next(
        r
        for r in read(previous / "live" / f"B-{prior_scope}.json")["rows"]
        if r["id"] == case["id"]
    )
    new_retrieval, old_retrieval = (events_of(r, "retrieval")[0] for r in (row, old))
    new_reader, old_reader = (events_of(r, "reader")[0] for r in (row, old))

    def source_and_ordinal(values):
        return [[ref["source"], ref["ordinal"]] for ref in values]

    prior_pool = source_and_ordinal(old_retrieval["candidate"])
    current_pool = source_and_ordinal(new_retrieval["candidate"])
    prior_visible = source_and_ordinal(old_reader["chunks"])
    current_visible = source_and_ordinal(new_reader["chunks"])
    return {
        "id": case["id"],
        "scope": case["scope"],
        "previous_receipt": f"evidence-retention-20261114/live/B-{prior_scope}.json",
        "same_ordered_candidate_sources_and_ordinals": prior_pool == current_pool,
        "same_ordered_reader_sources_and_ordinals": prior_visible == current_visible,
        "same_ordered_visible_text": [e["text"] for e in old_reader["entries"]]
        == [e["text"] for e in new_reader["entries"]],
        "previous_ordered_six": prior_visible,
        "current_ordered_six": current_visible,
        "previous_reader_sha256": old_reader["prompt_sha256"],
        "current_reader_sha256": new_reader["prompt_sha256"],
    }


def project(directory):
    p = read(directory / "protocol.json")
    cases = {c["id"]: c for c in p["cases"]}
    fields, matched, counts = {}, [], Counter()
    captured = {}
    for path in sorted((directory / "live").glob("*.json")):
        receipt = read(path)
        arm, scope = path.stem.split("-", 1)
        require(
            arm == receipt["arm"]
            and {r["id"] for r in receipt["rows"]}
            == {k for k, c in cases.items() if c["scope"] == scope},
            "Group identity differs",
        )
        for row in receipt["rows"]:
            if changed := pre_normalization(row, scope=scope, arm=arm):
                matched.append(changed)
            require((arm, row["id"]) not in captured, "Duplicate first HTTP response")
            captured[arm, row["id"]] = row
            counts[scope] += 1
        fields.setdefault(scope, {})[arm] = operational(receipt["rows"])
    require(
        counts == {"independent": 100, "historical": 4, "historical_vietnam": 2}, "HTTP denominator"
    )
    for cid, case in cases.items():
        a, b = [captured[arm, cid] for arm in ("A", "B")]
        expected = "A" if case["arm_order"][0] in ("baseline", "A") else "B"
        actual = "A" if datetime.fromisoformat(a["utc"]) < datetime.fromisoformat(b["utc"]) else "B"
        require(
            expected == actual and a["request_payload_sha256"] == b["request_payload_sha256"],
            "Dispatch schedule",
        )
    require(len({r["request_id"] for r in captured.values()}) == 106, "Request IDs")
    return {"rows": matched}, {
        "independent_first_http": 100,
        "historical_first_http": 6,
        "scopes": fields,
        "measured": operational(list(captured.values())),
        "paired_request_payload_equality": 53,
        "per_question_order_verified": 53,
        "first_http_retries": 0,
        "auth": "Independent HTTP sessions, a fresh JWT before each first dispatch.",
        "historical_rebuild": [
            historical_evidence(case, captured["B", cid], PRIOR)
            for cid, case in cases.items()
            if case["scope"] != "independent"
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    matched, summary = project(args.directory)
    write(args.directory / "matched-inputs.json", matched)
    write(args.directory / "provider-calls.json", summary)


if __name__ == "__main__":
    main()
