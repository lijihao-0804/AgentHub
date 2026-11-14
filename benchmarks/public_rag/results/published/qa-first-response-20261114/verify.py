"""Check published first HTTP receipts, scores and historical attribution with stdlib only."""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
sys.path.insert(0, str(ROOT))

from benchmarks.public_rag.qa_first_response_review import (  # noqa: E402
    assess,
    decision,
    index,
    packets,
    summarize,
)


def read(path):
    return json.loads(path.read_bytes())


def need(condition, label):
    if not condition:
        raise ValueError(label)


def validate(directory, protocol_name, arms, scopes):
    protocol = directory / protocol_name
    cases = index(read(protocol)["cases"])
    receipts = {}
    protocol_hash = hashlib.sha256(protocol.read_text(encoding="utf8").encode()).hexdigest()
    hashes, originals, count = {}, {}, 0
    for k, name in zip(("baseline", "candidate"), arms, strict=True):
        receipts[k], hashes[k] = [], {}
        for scope in scopes:
            blob = read(directory / f"{name}-{scope}.json")
            need(
                blob["arm"] == name and blob["protocol_sha256"] == protocol_hash, "receipt identity"
            )
            for r in blob["rows"]:
                cid = r["id"]
                need(r["state"] == "returned" and cases[cid]["scope"] == scope, "first response")
                count += 1
                reader = [e for e in r["events"] if e["kind"] == "reader"]
                wire = [e for e in r["events"] if e["kind"] == "wire"]
                need(len(reader) == 1 and wire, "evidence dispatch")
                need(
                    all(w["prompt_sha256"] == reader[0]["prompt_sha256"] for w in wire),
                    "model input",
                )
                hashes[k][cid] = reader[0]["prompt_sha256"]
                for c in r.get("citations", []):
                    expected = reader[0]["chunks"][c["number"] - 1]
                    need(
                        1 <= c["number"] and c["chunk_id"] == expected["chunk_id"], "citation scope"
                    )
                    need(
                        hashlib.sha256(c["excerpt"].encode()).hexdigest() == c["excerpt_sha256"],
                        "quote",
                    )
                    originals[(cid, k, c["number"])] = c["excerpt_sha256"]
            receipts[k].extend(blob["rows"])
    need(hashes["baseline"] == hashes["candidate"], "unequal paired reader input")
    masked, key = packets(cases, receipts)
    reported = read(directory / "review.json")
    marks = read(directory / "judgments.json")
    need(set(marks) == set(masked), "stale judgments")
    audited = {
        cid: {
            k: {"masked_id": uid[cid], **assess(masked[uid[cid]], marks[uid[cid]])}
            for k, uid in key.items()
        }
        for cid in cases
    }
    need(
        reported["rows"] == audited and reported["summary"] == summarize(audited, cases),
        "score totals",
    )
    summary = reported["summary"]
    primary = "reserve14" if "reserve14" in scopes else "independent"
    need(reported["assessment"] == decision(summary, primary), "preservation and citation gates")
    evidence = read(directory / "verification.json")
    need(evidence["paired_questions"] == len(cases), "pair count")
    totals = evidence["metrics"]["all"]
    need(
        sum(a["verified_original_citations"] for a in totals.values()) == len(originals),
        "source checks",
    )
    print(
        f"{directory.name}: {count} first HTTP responses, {len(cases)} paired inputs, "
        f"{len(originals)} citation hashes, {len(marks)} distinct reviewed responses"
    )


if __name__ == "__main__":
    history = read(HERE / "history/first-502.json")
    stages = Counter(r["stage"] for r in history["failures"])
    need(
        history["http_counts"] == {"502": 9, "200": 42}
        and stages
        == {"qa_unique_json_or_strict_output_schema": 5, "model_gateway_bad_response": 4},
        "historical evidence",
    )
    validate(
        HERE / "live", "protocol.json", ("baseline", "candidate"), ("independent", "qasper", "full")
    )
    validate(HERE / "budget", "protocol2.json", ("512", "2048"), ("reserve14", "qasper", "full"))
