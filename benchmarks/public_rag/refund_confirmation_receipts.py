"""Summarize the paid first runs, including refused tasks, missing runs and approval readback."""

import argparse
from collections import Counter
from pathlib import Path

from benchmarks.evaluation.experiments.support_refund_rag.product_review import reviewed_results
from benchmarks.public_rag.confirmation_cases import read, save
from benchmarks.public_rag.confirmation_receipts import reconcile


def candidate_coverage(record):
    if "evidence_groups" not in record["expected"]:
        return None
    traces = record.get("retrieval", [])
    measured = [t["candidate_group_complete"] for t in traces if "candidate_group_complete" in t]
    if any(measured):
        return True
    if len(measured) == len(traces):
        return False
    # A 401 interrupted the original diagnostics for S0-038. A complete visible
    # support group proves candidate coverage if every delivered ID is in one pool.
    visible = {s["chunk_id"] for s in record.get("visible", [])}
    if (
        visible
        and record.get("coverage")
        and any(
            visible <= {s["chunk_id"] for s in t["result"]["trace"]["selection"]["results"]}
            for t in traces
            if t["result"]["trace"].get("selection")
        )
    ):
        return True
    return None


def summarize(directory, reviews_path, ledger):
    capture = read(directory / "report.json")
    reviews = read(reviews_path)
    observed = reviewed_results(capture, reviews)
    charges = reconcile(ledger)
    rows = []
    for item in observed:
        key, stage = item["case_id"], item["stage"]
        row = {k: item.get(k) for k in ("case_id", "stage", "status", "run_id", "task_pass")}
        source = directory / (key + ".json")
        if not source.is_file():
            if item.get("status") != "NOT_ATTEMPTED":
                raise ValueError("Missing first-run receipt")
            rows.append(row)
            continue
        r = read(source)
        wires = [w for w in charges["wires"] if w["stage"] == stage and w["task"] == key]
        if wires != r["physical_attempts"]:
            raise ValueError("Run and shared ledger differ")
        citations = (r.get("citations") or {}).get("identities", [])
        review = reviews.get(key)
        if review is not None:
            support = review["citation_support"]
            if (
                set(support) != {i["id"] for i in citations}
                or any(type(value) is not bool for value in support.values())
                or any(support[i["id"]] and not i["visible"] for i in citations)
                or type(review["reported_refusal"]) is not bool
            ):
                raise ValueError("Incomplete refusal or citation-level review")
        row.update(
            run_status=r.get("run", {}).get("status"),
            failure_code=r.get("run", {}).get("failure_code"),
            thread_id=r.get("thread_id"),
            question=r["expected"]["input"],
            answerability=r["answerability"],
            answer=(r.get("turn") or {}).get("final_output")
            or (r.get("run") or {}).get("final_output"),
            contract_checks=r.get("contract_checks"),
            coverage=r.get("coverage"),
            candidate_group_complete=candidate_coverage(r),
            citations=citations,
            review=review,
            approval=(r.get("decision") or {}).get("approval"),
            ticket=r.get("ticket"),
            tickets_after=(r.get("tickets_after") or {}).get(
                "total", (r.get("reconciliation") or {}).get("postmortem_ticket_count")
            ),
            reconciliation=r.get("reconciliation"),
            wire_ids=[w["id"] for w in wires],
            settled_peak_microcny=sum(w["cost"] for w in wires if w["status"] == "returned"),
            unresolved=sum(w["status"] != "returned" for w in wires),
            end_to_end_seconds=r.get("end_to_end_seconds"),
        )
        rows.append(row)
    groups = {}
    for stage in ("refund_dev", "refund_new"):
        all_rows = [r for r in rows if r["stage"] == stage]
        tried = [r for r in all_rows if r.get("run_id")]
        assessed = [r for r in all_rows if r.get("review")]
        label_counts = Counter(r["answerability"] for r in tried)
        # Conflicts and missing information both require retaining uncertainty.
        negatives = [r for r in tried if r["answerability"] != "answerable"]
        positives = [r for r in tried if r["answerability"] == "answerable"]
        groups[stage] = {
            "planned": len(all_rows),
            "attempted": len(tried),
            "assessed": len(assessed),
            "labels": dict(label_counts),
            "label_negative_denominator": len(negatives),
            "correct": sum(r["task_pass"] for r in all_rows),
            "answerable_task_pass": sum(r["task_pass"] for r in positives),
            "contract_pass": sum(
                bool(r.get("contract_checks")) and all(r["contract_checks"].values()) for r in tried
            ),
            "run_succeeded": sum(r["run_status"] == "SUCCEEDED" for r in tried),
            "persisted_reply": sum(bool(r.get("answer")) for r in tried),
            "candidate_group_complete": sum(r["candidate_group_complete"] is True for r in tried),
            "candidate_group_assessed": sum(
                r["candidate_group_complete"] is not None for r in tried
            ),
            "visible_group_complete": sum(r["coverage"] is True for r in tried),
            "visible_group_assessed": sum(r["coverage"] is not None for r in tried),
            "reported_refusals": sum(r["review"]["reported_refusal"] for r in assessed),
            "correct_refusals": sum(
                bool(r.get("review") and r["review"]["correct_refusal"]) for r in negatives
            ),
            "correct_refusal_and_task_pass": sum(
                bool(r["task_pass"] and r.get("review") and r["review"]["correct_refusal"])
                for r in negatives
            ),
            "false_refusals": sum(
                bool(r.get("review") and r["review"]["false_refusal"]) for r in positives
            ),
            "cited_ids": sum(len(r["citations"]) for r in tried),
            "locatable_cited_ids": sum(sum(i["visible"] for i in r["citations"]) for r in tried),
            "scored_cited_ids": sum(len(r["review"]["citation_support"]) for r in assessed),
            "supported_cited_ids": sum(
                sum(r["review"]["citation_support"].values()) for r in assessed
            ),
            "condition_omissions": sum(r["review"]["condition_omission"] for r in assessed),
            "unsupported_affirmations": sum(
                r["review"]["unsupported_affirmation"] for r in assessed
            ),
            "failed_cases": [r["case_id"] for r in tried if not r["task_pass"]],
        }
    return {
        "execution_commit": capture["code_commit"],
        "execution_segments": capture.get("capture_segments"),
        "workspace_id": capture["workspace_id"],
        "groups": groups,
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("reviews", type=Path)
    parser.add_argument("ledger", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    save(args.out, summarize(args.capture, args.reviews, args.ledger))
