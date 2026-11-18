"""Summarize paired first replies and final HTTP outcomes from the publishable case records."""

import argparse
import json
from collections import Counter
from decimal import Decimal
from math import comb
from pathlib import Path


def fraction(numerator, denominator):
    return {
        "count": numerator,
        "total": denominator,
        "percent": round(100 * numerator / denominator, 2) if denominator else None,
    }


def metrics(rows, stage):
    positives = [row[stage] for row in rows if row["answerable"]]
    negatives = [row[stage] for row in rows if not row["answerable"]]
    items = positives + negatives
    delivered = [item for item in items if item["state"] == "answer" and item["cited_chunks"]]

    def ratio(group, field):
        return fraction(sum(item["scores"][field] for item in group), len(group))

    return {
        "strict_all": ratio(items, "joint_correct"),
        "positive_value": ratio(positives, "answer_correct"),
        "positive_supported": ratio(positives, "joint_correct"),
        "correct_refusal": ratio(negatives, "joint_correct"),
        "refusal_all": fraction(sum(item["state"] == "refusal" for item in items), len(items)),
        "false_refusals": ratio(positives, "wrong_refusal"),
        "cited_answer_support": ratio(delivered, "citation_correct"),
        "states": dict(Counter(item["state"] for item in items)),
    }


def deltas(pairs, stage, field, positive=None):
    eligible = {
        k: v
        for k, v in pairs.items()
        if positive is None or v["baseline"]["answerable"] == positive
    }
    improved, regressed = [], []
    for key, pair in sorted(eligible.items()):
        before, after = (pair[arm][stage]["scores"][field] for arm in ("baseline", "candidate"))
        if before != after:
            (improved if after else regressed).append(key)
    n = len(improved) + len(regressed)
    tail = sum(comb(n, k) for k in range(min(len(improved), len(regressed)) + 1))
    return {
        "improved": improved,
        "regressed": regressed,
        "unchanged_count": len(eligible) - n,
        "paired_exact_two_sided_p": min(1.0, 2 * tail / (2**n)),
    }


def summary(rows):
    pairs = {}
    for row in rows:
        group = pairs.setdefault(row["id"], {})
        if row["arm"] in group or row["attempts"] not in {1, 2}:
            raise ValueError("Duplicate arm or unexpected attempt count.")
        group[row["arm"]] = row
    for key, pair in pairs.items():
        if set(pair) != {"baseline", "candidate"} or any(
            pair["baseline"][k] != pair["candidate"][k] for k in ("evidence_sha256", "answerable")
        ):
            raise ValueError(f"Incomplete or incomparable pair {key}.")
    if len(pairs) != 100 or sum(p["baseline"]["answerable"] for p in pairs.values()) != 50:
        raise ValueError("Unexpected study denominator.")
    arms = {}
    for arm in ("baseline", "candidate"):
        side = [pair[arm] for pair in pairs.values()]
        arms[arm] = {
            "first": metrics(side, "first"),
            "final": metrics(side, "final"),
            "http_status": dict(Counter(row["http"] for row in side)),
            "confirmed_calls": sum(row["attempts"] for row in side),
            "corrections": sum(row["attempts"] - 1 for row in side),
            "estimated_cny": str(sum(Decimal(row["estimated_cny"]) for row in side)),
        }
    changes = {}
    for stage in ("first", "final"):
        changes[stage] = {
            "strict": deltas(pairs, stage, "joint_correct"),
            "positive_value": deltas(pairs, stage, "answer_correct", True),
            "positive_supported": deltas(pairs, stage, "joint_correct", True),
            "correct_refusal": deltas(pairs, stage, "joint_correct", False),
        }
    return {"arms": arms, "paired_changes": changes}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("records", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    rows = [json.loads(line) for line in args.records.read_bytes().splitlines()]
    args.output.write_text(json.dumps(summary(rows), indent=2) + "\n", encoding="utf8")
