"""Mask first responses and same-generation diagnostics, then score frozen paired outcomes."""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

from benchmarks.public_rag import evidence_retention_review as paired
from benchmarks.public_rag import qa_first_response_review as review
from benchmarks.public_rag.refusal_consistency_cases import OUT, lf_sha


def assess(item, mark):
    row = review.assess(item, mark)
    support = row["all_claims_supported"]
    positive = row["verdict"] in {"full", "partial", "wrong"}
    if (
        (positive and type(support) is not bool)
        or (not positive and support is not None)
        or (row["verdict"] == "full" and support is not True)
        or (support is True and not row["supporting_citations"])
    ):
        raise ValueError("Invalid whole-answer claim support")
    return row


def material_fields(mark):
    return mark["verdict"], frozenset(mark["supporting_citations"]), mark["all_claims_supported"]


def disputes(packet, first, second):
    if not (set(first) == set(second) == set(packet)):
        raise ValueError("Both independent reviews must cover every unique masked response")
    disputed, counts = {}, [0, 0, 0]
    for uid, item in packet.items():
        a, b = (material_fields(assess(item, marks[uid])) for marks in (first, second))
        fields = [x == y for x, y in zip(a, b, strict=True)]
        counts = [n + same for n, same in zip(counts, fields, strict=True)]
        if not all(fields):
            disputed[uid] = {"item": item, "first": first[uid], "second": second[uid]}
    audit = {
        "unique_masked_packets": len(packet),
        **{
            key: paired.rate(n, len(packet))
            for key, n in zip(
                ("verdict_agreement", "citation_set_agreement", "whole_answer_support_agreement"),
                counts,
                strict=True,
            )
        },
        "all_fields_agree": paired.rate(len(packet) - len(disputed), len(packet)),
        "adjudicated_masked_ids": sorted(disputed),
    }
    return disputed, audit


def masked_inputs(cases, rows, diagnostics):
    packet, keys = review.packets(cases, rows)
    raw_keys = {}
    for r in diagnostics:
        cid = r["id"]
        arm = {"A": "baseline", "B": "candidate", **{a: a for a in paired.ARMS}}[r["arm"]]
        if cid not in cases:
            continue  # A partial packet can be used to score finished pairs while the run proceeds.
        if r["scope"] != cases[cid]["scope"] or (cid, arm) in raw_keys:
            raise ValueError("Wrong diagnostic scope or duplicate question/arm")
        local, lookup = review.packets({cid: cases[cid]}, {arm: [r]})
        uid = lookup[arm][cid]
        if uid == keys[arm][cid] or (uid in packet and packet[uid] != local[uid]):
            raise ValueError("Unchanged raw output or colliding masked ID")
        raw_keys[cid, arm] = uid
        packet.update(local)
    return dict(sorted(packet.items())), keys, raw_keys


def semantic_summary(subset):
    s = paired.summary(subset)
    positives = {cid: row for cid, row in subset.items() if not row["baseline"]["negative"]}
    s["metrics"]["false_refusal_on_answerable"] = paired.paired(
        positives, lambda r: r["verdict"] == "refusal"
    )
    s["metrics"]["first_http_error"] = paired.paired(subset, lambda r: r["http"] != 200)
    values = {}
    for arm in paired.ARMS:
        returned = [r[arm] for r in subset.values() if r[arm]["all_claims_supported"] is not None]
        values[arm] = paired.rate(sum(r["all_claims_supported"] for r in returned), len(returned))
    s["metrics"]["all_claims_supported_per_substantive_response"] = {
        **values,
        "change_percentage_points": paired.delta(*(values[a] for a in paired.ARMS)),
    }
    return s


def compare_rate(metric, *, strict=False):
    old, new = (metric[a] for a in paired.ARMS)
    if not old["denominator"] or not new["denominator"]:
        return None
    a, b = (Fraction(v["numerator"], v["denominator"]) for v in (old, new))
    return b > a if strict else b >= a


def total_and_controls(scopes, rows):
    independent = scopes["independent"]["metrics"]
    correct = independent["first_answer_correct"]
    refusal = independent["correct_refusal"]
    controls = {
        cid: {arm: paired.correct(r[arm]) for arm in paired.ARMS}
        for cid, r in rows.items()
        if r["scope"] != "independent"
    }
    return {
        "independent": {
            "correctness_or_correct_refusal_improved": compare_rate(correct, strict=True)
            or compare_rate(refusal, strict=True),
            "first_answer_correctness_non_decreasing": compare_rate(correct),
            "correct_refusal_non_decreasing": compare_rate(refusal),
            "whole_answer_support_non_decreasing": compare_rate(
                independent["all_claims_supported_per_substantive_response"]
            ),
            "citation_precision_non_decreasing": compare_rate(independent["citation_precision"]),
            "all_baseline_correct_retained": not correct["lost"],
        },
        "historical_controls_correct": controls,
    }


def archive(directory, protocol, receipt_dir, diagnostics, write=False):
    if not (
        lf_sha(directory / "rubric.md") == protocol["whole_answer_rubric_sha256_lf"]
        and lf_sha(directory / "acceptance.md") == protocol["acceptance_sha256_lf"]
    ):
        raise ValueError("Blinded rubric or acceptance rules differ from the frozen protocol")
    cases, receipts = paired.get_rows(
        receipt_dir, protocol["cases"], lf_sha(directory / "protocol.json"), 0, None
    )
    if any(r["id"] not in cases for r in diagnostics):
        raise ValueError("Unfrozen diagnostic identity")
    packet, keys, raw = masked_inputs(cases, receipts, diagnostics)
    first, second, adjudication = (
        review.load(directory / name)
        for name in ("judgments-first.json", "judgments-second.json", "adjudication.json")
    )
    disputed, audit = disputes(packet, first, second)
    if set(disputed) != set(adjudication):
        raise ValueError("Resolve all and only disputed masked judgments")
    judgments = {**first, **adjudication}
    marks = {uid: assess(packet[uid], mark) for uid, mark in judgments.items()}
    details, diagnostic_rows = {}, []
    for cid, case in cases.items():
        details[cid] = {"scope": case["scope"], "query": case["query"]}
        for arm in paired.ARMS:
            key = keys[arm][cid]
            details[cid][arm] = {"masked_id": key, **marks[key]}
            if (uid := raw.get((cid, arm))) is not None:
                diagnostic_rows.append(
                    {
                        "id": cid,
                        "scope": case["scope"],
                        "arm": arm,
                        "raw": {"masked_id": uid, **marks[uid]},
                        "first_http": details[cid][arm],
                        "paired_correct_before": paired.correct(marks[uid]),
                        "paired_correct_after": paired.correct(marks[key]),
                    }
                )
    scopes = {
        s: semantic_summary({cid: r for cid, r in details.items() if r["scope"] == s})
        for s in dict.fromkeys(c["scope"] for c in cases.values())
    }
    result = {
        "protocol_sha256_lf": lf_sha(directory / "protocol.json"),
        "rubric_sha256_lf": lf_sha(directory / "rubric.md"),
        "unique_masked_answers": len(packet),
        "independent_first_http_responses": 2 * scopes["independent"]["n"],
        "historical_first_http_responses": 2 * (len(cases) - scopes["independent"]["n"]),
        "independent_review_agreement": audit,
        "scopes": scopes,
        "semantic_checks": total_and_controls(scopes, details),
        "same_terminal_output_diagnostics": diagnostic_rows,
        "rows": details,
    }
    for name, value in (("judgments.json", judgments), ("review.json", result)):
        path = directory / name
        if write and not path.exists():
            review.save(path, value)
        if review.load(path) != value:
            raise ValueError(f"{name} differs from recomputed paired judgments")
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--dir", type=Path, default=OUT)
    cli.add_argument("--receipts", type=Path)
    cli.add_argument("--raw", type=Path, help="Optional exported same-terminal diagnostics")
    modes = cli.add_mutually_exclusive_group(required=True)
    modes.add_argument("--packet", type=Path)
    modes.add_argument("--disputes", type=Path)
    modes.add_argument("--report", action="store_true")
    modes.add_argument("--check", action="store_true")
    cli.add_argument("--start", type=int, default=0)
    cli.add_argument("--count", type=int)
    args = cli.parse_args()
    if args.start < 0 or (args.count is not None and args.count < 1):
        cli.error("Packet selection must be positive")
    if not args.packet and (args.start or args.count is not None):
        cli.error("Partial cohorts are allowed only for masked batches")
    protocol = review.load(args.dir / "protocol.json")
    receipts = args.receipts or args.dir / "live"
    raw_path = args.raw or args.dir / "matched-inputs.json"
    diagnostics = review.load(raw_path) if raw_path.exists() else []
    if isinstance(diagnostics, dict):
        diagnostics = diagnostics["rows"]
    if args.report or args.check:
        if not raw_path.exists():
            cli.error("Complete reporting requires the same-terminal diagnostics export")
        result = archive(args.dir, protocol, receipts, diagnostics, write=args.report)
        print(json.dumps({k: result[k] for k in ("unique_masked_answers", "semantic_checks")}))
    else:
        cases, rows = paired.get_rows(
            receipts, protocol["cases"], lf_sha(args.dir / "protocol.json"), args.start, args.count
        )
        packet, _, _ = masked_inputs(cases, rows, diagnostics)
        if args.packet:
            review.save(args.packet, packet)
        else:
            first, second = (
                review.load(args.dir / f"judgments-{name}.json") for name in ("first", "second")
            )
            conflicts, _ = disputes(packet, first, second)
            review.save(args.disputes, conflicts)


if __name__ == "__main__":
    main()
