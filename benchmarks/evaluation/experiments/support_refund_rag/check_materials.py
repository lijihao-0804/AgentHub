"""Audit source-backed S0 artifacts, without retrieval or model dispatch."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
EXPECTED = {
    "single_policy": 10,
    "cross_condition": 10,
    "order_status": 10,
    "temporal_version": 8,
    "missing_conflict": 8,
    "injection": 4,
}


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def save(name: str, content: object) -> None:
    (ROOT / name).write_text(
        json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    manifest, cases, orders = (
        load(name) for name in ("manifest.json", "cases.json", "orders.json")
    )
    protocol = json.loads((ROOT / "protocol.json").read_text(encoding="utf-8"))
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    documents = manifest["documents"]
    questions = cases["questions"]
    by_order = {order["order_id"]: order for order in orders["orders"]}
    by_source = {doc["source_id"]: doc for doc in documents}
    require(12 <= len(documents) <= 20, "source count outside 12..20")
    require(len(by_source) == len(documents), "duplicate source IDs")
    require(len(by_order) == len(orders["orders"]), "duplicate order IDs")
    refund_ids = [
        order["refund"]["refund_id"] for order in by_order.values() if order.get("refund")
    ]
    require(len(refund_ids) == len(set(refund_ids)), "refund identity reused across orders")
    require(len(questions) == 50, "expected 50 questions")
    require(Counter(q["bucket"] for q in questions) == Counter(EXPECTED), "bucket count mismatch")
    require(len({q["case_id"] for q in questions}) == 50, "duplicate case IDs")
    require(len({q["input"] for q in questions}) == 50, "duplicate input texts")
    text_by_source = {}
    lengths = {}
    for doc in documents:
        source_id = doc["source_id"]
        path = (DATA / doc["path"]).resolve()
        require(path.is_relative_to((DATA / "docs").resolve()), f"{source_id}: not a policy path")
        if not path.is_relative_to((DATA / "docs").resolve()):
            continue
        body = path.read_text(encoding="utf-8")
        text_by_source[source_id] = body
        lengths[source_id] = len(body)
        require(
            doc["fictional"] is True and "fictional: true" in body, f"{source_id}: not fictional"
        )
        require(bool(doc["source_group"]), f"{source_id}: missing source group")
        require(source_id in body, f"{source_id}: missing document identity")
        require(
            bool(doc["scope"]["channels"] and doc["scope"]["products"]),
            f"{source_id}: missing scope",
        )
        start, end = (
            datetime.fromisoformat(doc[key]) for key in ("effective_from", "effective_to")
        )
        require(
            start.tzinfo is not None and end.tzinfo is not None and start <= end,
            f"{source_id}: invalid effective interval",
        )
        lifecycle = doc["lifecycle"]
        if lifecycle["status"] == "superseded":
            replacement = lifecycle["superseded_by"]
            require(
                replacement in by_source and replacement != source_id,
                f"{source_id}: invalid supersession target",
            )
    smoke = [q for q in questions if q["smoke_selected"]]
    require(len(smoke) == 10, "expected 10 smoke cases")
    require({q["bucket"] for q in smoke} == set(EXPECTED), "smoke misses a bucket")
    quote_count = 0
    for q in questions:
        label = q["case_id"]
        require(
            q["answerability"] in {"answerable", "insufficient_evidence", "conflicting_evidence"},
            f"{label}: disposition must not replace answerability",
        )
        named_orders = set(re.findall(r"ORD-S0-\d{3}", q["input"]))
        require(
            named_orders == {q["order_fixture"]["order_id"]},
            f"{label}: input order and lookup fixture differ",
        )
        require(q["review_status"] == "assistant-reviewed", f"{label}: wrong reviewer provenance")
        require(q["order_fixture"]["order_id"] in by_order, f"{label}: missing order fixture")
        require(
            datetime.fromisoformat(q["business_time"]).tzinfo is not None,
            f"{label}: business time lacks timezone",
        )
        require(
            q["business_time"] == protocol["business_clock"]["as_of"],
            f"{label}: protocol clock mismatch",
        )
        require(q["evidence_group_logic"] == "outer_OR_inner_AND", f"{label}: wrong evidence logic")
        require(
            bool(q["key_facts"] and q["forbidden_promises"] and q["expected_disposition"]),
            f"{label}: missing semantic rubric",
        )
        require(bool(q["evidence_groups"]), f"{label}: missing evidence/context for abstention")
        for group in q["evidence_groups"]:
            require(bool(group["all_of"]), f"{label}: empty evidence group")
            for evidence in group["all_of"]:
                quote_count += 1
                require(
                    bool(evidence["quote"])
                    and evidence["quote"] in text_by_source.get(evidence["source_id"], ""),
                    f"{label}: quote absent from {evidence['source_id']}",
                )
    # A strict allowlist creates an input inventory, not a real admission view.
    inputs = [{key: q[key] for key in ("case_id", "input", "business_time")} for q in questions]
    save(
        "material-audit.json",
        {
            "status": "PASS" if not errors else "FAIL",
            "scope": "offline material integrity only",
            "semantic_accuracy": "not established by this script; see source-review.md",
            "sources": len(documents),
            "orders": len(by_order),
            "cases": len(questions),
            "buckets": dict(Counter(q["bucket"] for q in questions)),
            "smoke_ids": [q["case_id"] for q in smoke],
            "verified_quote_occurrences": quote_count,
            "source_char_lengths": lengths,
            "errors": errors,
            "model_dispatches": 0,
            "retrieval_executions": 0,
            "hashes_added": 0,
        },
    )
    if errors:
        raise SystemExit("Material audit FAIL: " + "; ".join(errors))
    save("model-inputs.json", inputs)
    # Never overwrite an execution result; this is only the pre-dispatch denominator.
    plan_path = ROOT / "planned-results.json"
    if not plan_path.exists():
        save(
            "planned-results.json",
            {
                "status": "NOT_ATTEMPTED",
                "planned": 50,
                "physical_dispatches": 0,
                "quality_metrics": None,
                "billing_status": "NO_EXPERIMENT_DISPATCH",
                "assistant_authoring_review_cost": "UNKNOWN",
                "cases": [
                    {
                        "case_id": q["case_id"],
                        "bucket": q["bucket"],
                        "A": "NOT_ATTEMPTED",
                        "B": "NOT_ATTEMPTED",
                        "score": None,
                    }
                    for q in questions
                ],
            },
        )
    print(
        f"PASS: {len(documents)} sources, 50 cases, 10 smoke cases, "
        f"{quote_count} exact quotes; zero dispatches."
    )


if __name__ == "__main__":
    main()
