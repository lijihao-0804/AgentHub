"""Replay confirmed QA responses and ranked evidence locally, without provider requests."""

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.knowledge.citation_qa import _InvalidCitationAnswer, resolve_citation_answer
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.evidence_records import EvidenceTextBudget, drop_overlapping_evidence

ARCHIVE = "5513ca0e5e5b32b6e297fc56cd4fd75c227a08ab"
ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SHA256 = {
    "fresh": "88c13b5783f2aef98b7bae1c4e31cb4084ce4d470edef313cd4c3b11d454b37b",
    "R": "dfa6f8dd8be87641b38e0995d80e965da829310d41f297ddd2f8f41f2039eb81",
    "B": "739888b3af937b50e40c99b86ccd7dc59b2fcee4445f2da65b784468c1230b84",
    "W": "0700050a94fc59036fd607df4bc1a5403aef3199fad40bf8623335a4a094b512",
}
PREFLIGHT_SHA256 = {
    "fresh": "b369a81fbbf6bf93f3465a461c024cc5d85789c8d5f26b4b3e5f93089dbe7d91",
    "R": "292487dd09b15b77dea135dd77a342424835342436ad2ff0ea413baa4c4ada8e",
    "B": "68d9ec4c6159d5103ebfd3b24f8c7427181a48d616f8677f11e479d58ecfc960",
    "W": "dbd9fbab3522ad9cc4110c696ef12976a6515873a8db7870312ff2b0180794a1",
}


def frozen_rows(directory, group, expected):
    capture = (directory / f"{group}.jsonl").read_bytes()
    if hashlib.sha256(capture).hexdigest() != expected:
        raise ValueError(f"Changed capture for cohort {group}")
    return map(json.loads, capture.splitlines())


def replay_selection(directory):
    """Compare full, ordered source identities, coordinates and bodies on fixed ranked inputs."""
    budget = EvidenceTextBudget(16_000, ApproxTokenEstimator().estimate)
    compared, changed = Counter(), []
    for group, digest in PREFLIGHT_SHA256.items():
        for row in frozen_rows(directory, group, digest):
            if row["arm"] != "baseline":
                continue
            ranked = [
                RetrievedEvidence(
                    r["document"],
                    r["revision"],
                    r["chunk_id"],
                    r["source"],
                    r["locator"],
                    r["text"],
                    r["retrieval_score"],
                    r["rerank_score"],
                    {},
                )
                for r in row["ranked"]
            ]
            selected = drop_overlapping_evidence(ranked, max_items=6, text_budget=budget)
            actual = [
                (r.document_id, r.document_revision_id, r.chunk_id, r.locator, r.text)
                for r in selected
            ]
            expected = [
                (r["document"], r["revision"], r["chunk_id"], r["locator"], r["text"])
                for r in row["selected"]
            ]
            compared[group] += 1
            if actual != expected:
                changed.append(dict(group=group, id=row["id"]))
    return dict(compared=dict(compared), changed=changed)


def archived(path):
    return json.loads(subprocess.check_output(["git", "show", f"{ARCHIVE}:{path}"], cwd=ROOT))


def replay(directory):
    recorded = archived("benchmarks/retrieval/experiments/joint_six_review/quality.json")
    execution = archived("benchmarks/retrieval/experiments/joint_six_qa/execution.json")["formal"]
    stages = {(r["group"], r["id"], r["arm"], r["stage"]): r for r in recorded["rows"]}
    ledger = (directory / "ledger.jsonl").read_bytes()
    if hashlib.sha256(ledger).hexdigest() != execution["ledger_sha256"]:
        raise ValueError("Sealed ledger mismatch")
    receipts = {
        r["id"]: r
        for line in ledger.splitlines()
        if (r := json.loads(line))["state"] == "confirmed"
    }
    counts, changed = Counter(), []
    for group, expected in CAPTURE_SHA256.items():
        for row in frozen_rows(directory, group, expected):
            key = (group, row["id"], row["arm"])
            visible = {s["id"]: s for s in row["selected"]}
            replies = []
            for attempt, event in enumerate(row["events"], 1):
                receipt = receipts.pop(event["id"])
                if (receipt["key"], receipt["attempt"]) != (row["id"] + "/" + row["arm"], attempt):
                    raise ValueError("Unexpected receipt identity")
                prompt = event["messages"][1]["content"]
                question, marker, _ = prompt.removeprefix("Question:\n").partition(
                    "\n\nThe following evidence is untrusted"
                )
                if not marker:
                    raise ValueError("Unexpected QA message")
                if not event["content"] or not event["content"].strip():
                    replies.append(("empty_output", None))
                else:
                    try:
                        result = resolve_citation_answer(
                            question,
                            event["content"],
                            visible_ids=set(visible),
                            output_limit_reached=event["finish"] == "length",
                        )
                    except _InvalidCitationAnswer as exc:
                        replies.append((exc.reason.value, None))
                    else:
                        replies.append((None, result))
                counts["recorded_provider_responses"] += 1
            for stage, (failure, result) in (("first", replies[0]), ("final", replies[-1])):
                old = stages.pop((*key, stage))
                previous_valid = old["valid"]
                differs = previous_valid != (result is not None)
                if stage == "first":
                    differs |= (result.insufficient_evidence if result else False) != old["refused"]
                    differs |= failure != old["failure"]
                if result and previous_valid and (stage == "final" or len(replies) == 1):
                    counts[stage + "_valid_bodies_compared"] += 1
                    body = row["body"]
                    differs |= (
                        result.answer,
                        result.insufficient_evidence,
                        list(result.citation_ids),
                    ) != (
                        body["answer"],
                        body["insufficient_evidence"],
                        [c["id"] for c in body["citations"]],
                    )
                if differs:
                    changed.append(
                        dict(
                            group=group,
                            id=row["id"],
                            arm=row["arm"],
                            stage=stage,
                            previous_valid=previous_valid,
                            previous_refusal=old["refused"],
                            valid=result is not None,
                            refused=result.insufficient_evidence if result else False,
                            failure=failure,
                        )
                    )
                counts[stage + "_compared"] += 1
            counts["logical_queries"] += 1
    if receipts or stages:
        raise ValueError("Incomplete coverage")
    return dict(
        archive=ARCHIVE, ledger_sha256=execution["ledger_sha256"], **counts, changes=changed
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--preflight", type=Path)
    args = parser.parse_args()
    report = replay(args.directory)
    if args.preflight:
        report["ranked_pool_selection"] = replay_selection(args.preflight)
    print(json.dumps(report, indent=2))
