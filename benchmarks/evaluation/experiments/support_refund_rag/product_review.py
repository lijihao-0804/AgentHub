"""Adjudicate a frozen paid capture against visible policy, business facts and receipts."""

import argparse
import json
from pathlib import Path

AXES = (
    "correct",
    "necessary_conditions_complete",
    "visible_support",
    "citation_semantics",
    "false_refusal",
    "correct_refusal",
    "unsupported_affirmation",
    "condition_omission",
)
GOOD = AXES[:4]
BAD = ("false_refusal", "unsupported_affirmation", "condition_omission")


def reviewed_results(capture, reviews):
    rows = []
    expected = {
        row["case_id"]
        for row in capture["rows"]
        if row.get("contract_pass") is not None and row.get("semantic_verdict") != "RUN_ERROR"
    }
    if capture["mode"] != "paid" or set(reviews) != expected:
        raise ValueError("one independent source-grounded review per observed paid case required")
    for row in capture["rows"]:
        if row["case_id"] not in expected:
            rows.append({**row, "task_pass": False, "status": "ERROR"})
            continue
        review = reviews[row["case_id"]]
        if any(type(review.get(key)) is not bool for key in AXES) or not review.get("reason"):
            raise ValueError("incomplete review axes or rationale")
        uncited = bool(row.get("uncited_missing_fact_review"))
        justified_refusal = (
            uncited
            and row.get("answerability") == "insufficient_evidence"
            and review["correct_refusal"]
            and not review.get("citations")
            and bool(review.get("business_evidence"))
            and review.get("policy_claims") == []
        )
        if review["visible_support"] and not review.get("citations") and not justified_refusal:
            raise ValueError("visible support requires cited locators")
        rows.append(
            {
                **row,
                "review": review,
                "task_pass": bool(row["contract_pass"])
                and (justified_refusal or not uncited)
                and all(review[key] for key in GOOD[:3])
                and (review["citation_semantics"] or justified_refusal)
                and not any(review[key] for key in BAD),
            }
        )
    attempted = {row["case_id"] for row in rows}
    rows.extend(
        {"case_id": key, "stage": stage, "task_pass": False, "status": "NOT_ATTEMPTED"}
        for key, stage in capture.get("schedule", {}).items()
        if key not in attempted
    )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("reviews", type=Path)
    args = parser.parse_args()
    report = json.loads((args.capture / "report.json").read_text(encoding="utf8"))
    reviews = json.loads(args.reviews.read_text(encoding="utf8"))
    for key, review in reviews.items():
        record = json.loads((args.capture / (key + ".json")).read_text(encoding="utf8"))
        visible = {r["chunk_id"]: r for r in record["visible"]}
        messages = record["calls"][-1]["request"]["messages"] if record.get("calls") else []
        for fact in review.get("business_evidence", []):
            if fact["tool"] not in {"get_order", "get_refund_status", "query_customer"}:
                raise ValueError("unknown business evidence locator")
            if not fact["quote"] or not any(
                m["role"] == "tool" and m["name"] == fact["tool"] and fact["quote"] in m["content"]
                for m in messages
            ):
                raise ValueError("business evidence is absent from the final request")
        for citation in review.get("citations", []):
            item = visible.get(citation["chunk_id"])
            if not item or not citation["quote"] or citation["quote"] not in item["snippet"]:
                raise ValueError("reviewed quote is absent from the final provider request")
    print(json.dumps(reviewed_results(report, reviews), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
