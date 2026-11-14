"""Check public receipts and the pinned provenance manifest with the standard library.

The full reader --verification CLI also rebuilds source identities from Git
and validates citation bytes against the original QASPER and SciFact corpora.
"""

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


def validate_reader(directory, *, supplement=False):
    from benchmarks.public_rag import qa_first_response_reader_review as check

    inputs = check.supplement_inputs if supplement else check.primary_inputs
    _, cases, groups = inputs(directory)
    masked, keys = check.blind(cases, groups)
    marks = read(directory / "judgments.json")
    need(
        check.report(cases, masked, keys, marks, supplement=supplement)
        == read(directory / "review.json"),
        "three-arm decisions",
    )
    quotes, http = 0, 0
    paired = {}
    coverage = check.auth_coverage(groups)
    evidence = read(directory / "verification.json")
    path = (directory.parent if supplement else directory) / "prompt-provenance.json"
    need(
        hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        == "1550a637b623ed8afca237532e6323ce86bcc568bcc61f5c1935ad0d66290fd2",
        "frozen public source manifest",
    )
    provenance = read(path)
    need(evidence["source_provenance"] == provenance, "recorded source provenance")
    for arm in check.ARMS:
        label = "strict" if arm == "C" else "original"
        for receipt in groups[arm].values():
            need(receipt["source"] == provenance[label]["source_raw_sha256"], "source receipt")
            for row in receipt["rows"]:
                http += 1
                cid = row["id"]
                if row["http"] == 401:
                    audited = evidence["rows"][arm][cid]
                    need(
                        audited["authenticated"] is False
                        and audited["http"] == 401
                        and audited["wire_attempts"] == 0
                        and not audited["verified_references"]
                        and not audited["verified_citations"]
                        and not audited["candidate"]
                        and not audited["reader"]
                        and not audited["responses"]
                        and not audited["adapter_errors"]
                        and audited["failure_observation"] == "authentication:ACCESS_TOKEN_EXPIRED"
                        and not any(
                            k in audited for k in ("digest", "system_prompt_sha256", "prompt_sha")
                        ),
                        "No retrieval or provider evidence for an expired token",
                    )
                    continue
                (reader,) = (e for e in row["events"] if e["kind"] == "reader")
                (retrieval,) = (e for e in row["events"] if e["kind"] == "retrieval")
                fingerprint = (
                    reader["prompt_sha256"],
                    reader["chunks"],
                    retrieval["candidate"],
                    retrieval["reranked"],
                )
                need(fingerprint == paired.setdefault(cid, fingerprint), "three-arm evidence")
                wire = [e for e in row["events"] if e["kind"] == "wire"]
                audited = evidence["rows"][arm][cid]
                need(
                    audited["http"] == row["http"]
                    and audited["authenticated"] is True
                    and audited["wire_attempts"] == len(wire)
                    and audited["prompt_sha"] == reader["prompt_sha256"]
                    and audited["system_prompt_sha256"] == check.PROMPTS[label]
                    and audited["verified_citations"] == len(row.get("citations", []))
                    and audited["verified_references"]
                    == [
                        {k: v for k, v in cit.items() if k != "excerpt"}
                        for cit in row.get("citations", [])
                    ],
                    "recorded wire and exact citation identities",
                )
                need(
                    wire
                    and all(
                        w["system_prompt_sha256"] == check.PROMPTS[label]
                        and w["max_tokens"] == (512 if arm == "A" else 2048)
                        and w["timeout_seconds"] == 60
                        and w["retry_limit"] == 0
                        and w["temperature"] == 0
                        and w["enable_thinking"] is None
                        and w["prompt_sha256"] == reader["prompt_sha256"]
                        for w in wire
                    ),
                    "three-arm wire",
                )
                for c in row.get("citations", []):
                    n = c["number"]
                    need(
                        1 <= n <= len(reader["chunks"])
                        and reader["chunks"][n - 1]["chunk_id"] == c["chunk_id"]
                        and hashlib.sha256(c["excerpt"].encode()).hexdigest()
                        == c["excerpt_sha256"],
                        "three-arm citation receipt",
                    )
                    quotes += 1
    need(
        evidence["paired_questions"] == len(cases)
        and evidence["auth_coverage_by_scope"] == coverage
        and evidence["authenticated_pairs"] == len(paired)
        and coverage[check.PRIMARY]["paired_questions"] == (4 if supplement else 16)
        and coverage[check.PRIMARY]["authenticated_pairs"] == (4 if supplement else 12)
        and sum(s["verified_original_citations"] for s in evidence["metrics"]["all"].values())
        == quotes,
        "three-arm original-source counts",
    )
    print(
        f"{directory.name}: {http} first HTTP responses, {len(cases)} matched triples, "
        f"{len(paired)} authenticated triples, {quotes} citation hashes, "
        f"{len(marks)} distinct reviewed responses"
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
    validate_reader(HERE / "reader")
    validate_reader(HERE / "reader" / "authenticated-supplement", supplement=True)
