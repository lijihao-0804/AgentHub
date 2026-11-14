"""Mask the first HTTP answers, then recompute paired semantic metrics from judgments."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from fractions import Fraction
from math import comb
from pathlib import Path

from benchmarks.public_rag import qa_first_response_review as review
from benchmarks.public_rag.evidence_retention_cases import OUT, digest

ARMS = ("baseline", "candidate")
LABELS = {"baseline": "A", "candidate": "B"}
SCOPES = ("independent", "historical")


def get_rows(directory, cases, protocol_sha, start, count):
    selected = cases[start : start + count] if count is not None else cases[start:]
    if not selected:
        raise ValueError("Empty review selection")
    ids = {case["id"] for case in selected}
    result = {}
    for arm, letter in LABELS.items():
        rows = []
        for scope in dict.fromkeys(c["scope"] for c in selected):
            needed = [c["id"] for c in selected if c["scope"] == scope]
            group_file = directory / f"{letter}-{scope}.json"
            paths = (
                [group_file]
                if group_file.exists()
                else [directory / f"{letter}-{k}.json" for k in needed]
            )
            for path in paths:
                receipt = review.load(path)
                items = review.index(receipt["rows"])
                expected = (
                    {c["id"] for c in cases if c["scope"] == scope}
                    if path == group_file
                    else {path.stem.removeprefix(letter + "-")}
                )
                if (
                    receipt["arm"] not in {arm, letter}
                    or receipt["endpoint"] != "POST /api/v1/knowledge/query"
                    or receipt["protocol_sha256"] != protocol_sha
                    or set(items) != expected
                ):
                    raise ValueError(f"Receipt identity mismatch at {path}")
                rows.extend(r for qid, r in items.items() if qid in ids)
        if set(review.index(rows)) != ids or any(r["state"] != "returned" for r in rows):
            raise ValueError(f"Missing or unfinished first HTTP receipt for {arm}")
        result[arm] = rows
    return review.index(selected), result


def rate(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percent": round(100 * numerator / denominator, 3) if denominator else None,
    }


def delta(old, new):
    if not old["denominator"] or not new["denominator"]:
        return None
    change = Fraction(new["numerator"], new["denominator"]) - Fraction(
        old["numerator"], old["denominator"]
    )
    return round(float(100 * change), 3)


def exact_p(gains, losses):
    discordant = gains + losses
    return min(
        1, 2 * sum(comb(discordant, j) for j in range(min(gains, losses) + 1)) / 2**discordant
    )


def paired(subset, predicate):
    old, new = ({i for i, r in subset.items() if predicate(r[arm])} for arm in ARMS)
    lost, gained, retained = sorted(old - new), sorted(new - old), sorted(old & new)
    before, after = (rate(len(group), len(subset)) for group in (old, new))
    return {
        "baseline": before,
        "candidate": after,
        "change_percentage_points": delta(before, after),
        "retained": retained,
        "gained": gained,
        "lost": lost,
        "neither_count": len(subset) - len(old | new),
        "retention_of_baseline_hits": rate(len(retained), len(old)),
        "two_sided_exact_p": exact_p(len(gained), len(lost)) if subset else None,
    }


def correct(row):
    return row["verdict"] == ("refusal" if row["negative"] else "full")


def wrong_positive(row):
    return row["verdict"] == "wrong" or (row["negative"] and row["verdict"] in {"full", "partial"})


def summary(subset):
    positive = {i: r for i, r in subset.items() if not r["baseline"]["negative"]}
    negative = {i: r for i, r in subset.items() if r["baseline"]["negative"]}
    metrics = {
        "first_http_200": paired(subset, lambda r: r["http"] == 200),
        "first_answer_correct": paired(subset, correct),
        "fully_supported_positive": paired(positive, correct),
        "correct_refusal": paired(negative, correct),
        "wrong_positive": paired(subset, wrong_positive),
        "official_negative_answered": paired(
            negative, lambda r: r["verdict"] in {"full", "partial", "wrong"}
        ),
    }
    precision, distributions = {}, {}
    for arm in ARMS:
        rows = [v[arm] for v in subset.values()]
        precision[arm] = rate(
            sum(len(r["supporting_citations"]) for r in rows),
            sum(r["returned_citations"] for r in rows),
        )
        distributions[arm] = {
            "http_status": dict(sorted(Counter(r["http"] for r in rows).items())),
            "answerable": dict(
                sorted(Counter(r["verdict"] for r in rows if not r["negative"]).items())
            ),
            "official_negative": dict(
                sorted(Counter(r["verdict"] for r in rows if r["negative"]).items())
            ),
            "source_label_conflict_ids": sorted(
                cid for cid, r in negative.items() if r[arm]["verdict"] in {"full", "partial"}
            ),
        }
    before, after = (precision[arm] for arm in ARMS)
    metrics["citation_precision"] = {**precision, "change_percentage_points": delta(before, after)}
    return {"n": len(subset), "metrics": metrics, "outcomes": distributions}


def report(cases, rows, marks):
    masked, keys = review.packets(cases, rows)
    if set(marks) != set(masked):
        raise ValueError("Missing or stale masked judgments")
    details = {}
    for cid in cases:
        details[cid] = {}
        for arm in ARMS:
            uid = keys[arm][cid]
            details[cid][arm] = {"masked_id": uid, **review.assess(masked[uid], marks[uid])}
    scopes = {
        s: summary({cid: r for cid, r in details.items() if cases[cid]["scope"] == s})
        for s in SCOPES
    }
    m = scopes["independent"]["metrics"]
    cq = m["citation_precision"]
    o, n = (cq[a] for a in ARMS)
    return {
        "judging": (
            "One arm-masked assistant review of unique actual answers with their public returned "
            "excerpts and official annotations. Identical packet contents share a judgment. "
            "Each supporting citation grounds at least one substantive claim attached to it. "
            "Exact paired p values are descriptive for this selected TRAIN cohort."
        ),
        "unique_masked_answers": len(masked),
        "first_http_receipts": sum(len(v) for v in rows.values()),
        "scopes": scopes,
        "independent_semantic_gate": {
            "first_answer_correctness_non_decreasing": (
                m["first_answer_correct"]["candidate"]["numerator"]
                >= m["first_answer_correct"]["baseline"]["numerator"]
            ),
            "all_baseline_full_positives_retained": not m["fully_supported_positive"]["lost"],
            "all_baseline_correct_refusals_retained": not m["correct_refusal"]["lost"],
            "no_new_wrong_positive": not m["wrong_positive"]["gained"],
            "citation_precision_non_decreasing": (
                Fraction(n["numerator"], n["denominator"])
                >= Fraction(o["numerator"], o["denominator"])
                if o["denominator"] and n["denominator"]
                else None
            ),
        },
        "rows": details,
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "receipts", type=Path, help="Directory of per-case or per-scope A/B first responses"
    )
    cli.add_argument("--protocol", default=OUT / "protocol.json", type=Path)
    cli.add_argument("--packet", type=Path, help="Output of masked questions, answers and excerpts")
    cli.add_argument("--start", type=int, default=0, help="First case offset for a masked packet")
    cli.add_argument("--count", type=int, help="Number of consecutive cases for a masked packet")
    cli.add_argument("--judgments", type=Path)
    cli.add_argument("--report", type=Path)
    args = cli.parse_args()
    if (
        not (args.packet or args.report)
        or bool(args.judgments) != bool(args.report)
        or (args.report and (args.start or args.count is not None))
        or args.start < 0
        or (args.count is not None and args.count < 1)
    ):
        cli.error(
            "Write a packet or supply both judgments and a report; reports require all cases."
        )
    protocol = review.load(args.protocol)
    sha = digest(args.protocol.read_text(encoding="utf8").encode())
    cases, rows = get_rows(args.receipts, protocol["cases"], sha, args.start, args.count)
    if args.packet:
        packet, _ = review.packets(cases, rows)
        review.save(args.packet, packet)
    if args.report:
        result = report(cases, rows, review.load(args.judgments))
        result["protocol_sha256_lf"] = sha
        review.save(args.report, result)
        print(json.dumps(result["independent_semantic_gate"]))


if __name__ == "__main__":
    main()
