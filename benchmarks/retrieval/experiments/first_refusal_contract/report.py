"""Summarize the preregistered first-refusal pairs without publishing private content."""

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from benchmarks.retrieval.experiments.output_limit.report import money, rate

from .scoring import ARMS, DIRECTORY, ROOT, STAGES, lines, load, save

NAMED = {"N035", "W13", "2hop__8556_47169", "B097", "R092", "B006"}
FAILURES = NAMED - {"R092", "B006"}
HISTORY = ROOT / "benchmarks/retrieval/experiments/output_limit/results/row-status.jsonl"
FIELDS = ("valid", "refused", "value", "support", "language", "strict", "reason")


def receipt_cost(event):
    usage = event["usage"]
    inp, out = usage["prompt_tokens"], usage["completion_tokens"]
    cached = usage["prompt_tokens_details"]["cached_tokens"]
    if not (0 <= cached <= inp and 0 <= out <= 2048 and inp + out == usage["total_tokens"]):
        raise ValueError("Receipt usage falls outside the frozen budget")
    hour = datetime.fromtimestamp(event["created"], timezone(timedelta(hours=8))).hour
    return ((inp - cached) * 100 + cached * 2 + out * 400) * (2 if 8 <= hour < 22 else 1)


def rows_from(outcomes, grades):
    by_key = {(r["id"], r["arm"], r["stage"]): r for r in grades}
    if len(by_key) != len(grades) or len(grades) != 2 * len(outcomes):
        raise ValueError("Stage grades do not cover every first and delivered answer")
    rows = []
    for r in outcomes:
        first = by_key[r["id"], r["arm"], "first"]
        rows.append(
            dict(
                id=r["id"],
                qid=r["qid"],
                arm=r["arm"],
                mode=r["mode"],
                cohort="archive"
                if r["mode"] == "archive"
                else "named"
                if r["qid"] in NAMED
                else "independent",
                answerable=first["answerable"],
                language=first["language_expected"],
                http=r["http"],
                model_declared_refusal=first["raw_model_declared_refusal"],
                model_cited_a_declared_refusal=first["raw_cited_refusal"],
                normalized_by=first["rule"],
                invocations=len(r["events"]),
                cost_units=sum(receipt_cost(e) for e in r["events"]),
                **{s: {k: by_key[r["id"], r["arm"], s][k] for k in FIELDS} for s in STAGES},
            )
        )
    return rows


def summarize(rows):
    negative = [r for r in rows if not r["answerable"]]
    positive = [r for r in rows if r["answerable"]]
    calls = sum(r["invocations"] for r in rows)
    result = dict(
        questions=len(rows),
        distinct_qids=len({r["qid"] for r in rows}),
        model_path_calls=calls,
        corrections=calls - len(rows),
        cost_at_full_receipt_rates_cny=money(sum(r["cost_units"] for r in rows)),
        raw_first_model_declared_refusal=sum(r["model_declared_refusal"] is True for r in rows),
        raw_first_cited_refusals=sum(r["model_cited_a_declared_refusal"] for r in rows),
    )
    for stage in STAGES:
        substantive = [r for r in rows if r[stage]["valid"] and not r[stage]["refused"]]
        result[stage] = dict(
            strict=rate(sum(r[stage]["strict"] for r in rows), len(rows)),
            correct_refusals=rate(
                sum(r[stage]["strict"] and r[stage]["refused"] for r in negative), len(negative)
            ),
            false_refusals=rate(sum(r[stage]["refused"] for r in positive), len(positive)),
            citation_support=rate(sum(r[stage]["support"] for r in substantive), len(substantive)),
            supported_answerable=sum(
                r[stage]["valid"] and not r[stage]["refused"] and r[stage]["support"]
                for r in positive
            ),
            invalid_reasons=dict(
                Counter(r[stage]["reason"] for r in rows if not r[stage]["valid"])
            ),
        )
    return result


def compare(rows):
    by = {(r["id"], r["arm"]): r for r in rows}
    ids = sorted({r["id"] for r in rows})
    result = {"arms": {a: summarize([r for r in rows if r["arm"] == a]) for a in ARMS}}
    for stage in STAGES:
        gains, losses, refusal_losses, false_reclass = [], [], [], []
        for key in ids:
            baseline, candidate = (by[key, a] for a in ARMS)
            old, new = baseline[stage], candidate[stage]
            if new["strict"] and not old["strict"]:
                gains.append(key)
            if old["strict"] and not new["strict"]:
                losses.append(key)
                if old["refused"]:
                    refusal_losses.append(key)
            if (
                baseline["answerable"]
                and old["valid"]
                and not old["refused"]
                and old["support"]
                and new["refused"]
            ):
                false_reclass.append(key)
        result[stage] = dict(
            gains=gains,
            losses=losses,
            correct_refusal_losses=refusal_losses,
            supported_answer_to_refusal=false_reclass,
        )
    return result


def build(rows, outcomes):
    by, summary = {}, {}
    for cohort in ("independent", "named", "archive"):
        subset = [r for r in rows if r["cohort"] == cohort]
        by[cohort] = subset
        summary[cohort] = compare(subset)
    summary["independent_by_language"] = {
        lang: compare([r for r in by["independent"] if r["language"] == lang])
        for lang in ("zh", "en")
    }
    ind = summary["independent"]["arms"]
    live_ids = {r["id"] for r in by["independent"]}
    split = Counter(
        (r["language"], r["answerable"]) for r in by["independent"] if r["arm"] == "baseline"
    )
    if len(live_ids) != 64 or set(split.values()) != {16} or len(split) != 4:
        raise ValueError(
            "Independent cohort differs from the registered language/answerability strata"
        )
    old_rows = {r["id"]: r for r in lines(HISTORY) if r["cap"] == 2048}
    archive = {r["id"].removeprefix("archive/"): r for r in by["archive"] if r["arm"] == "baseline"}
    expected = {
        k
        for k, r in old_rows.items()
        if r["first"]["strict"] or r["final"]["strict"] or r["qid"] in NAMED
    }
    if set(archive) != expected:
        raise ValueError("An old protected identity is missing from the archive replay")
    if any(archive[k][s]["strict"] != old_rows[k][s]["strict"] for k in archive for s in STAGES):
        raise ValueError("Historical strict grades or baseline behavior changed")
    named = [r for r in by["named"] if r["arm"] == "candidate"]
    diagnostic = [r for r in by["archive"] if r["arm"] == "candidate" and r["qid"] in FAILURES]
    if len(named) != 7 or {r["qid"] for r in named} != NAMED or len(diagnostic) != 5:
        raise ValueError("Named input coverage incomplete")
    groups = [summary[k] for k in by]
    checks = dict(
        independent_first_strict_net_gain=ind["candidate"]["first"]["strict"]["n"]
        > ind["baseline"]["first"]["strict"]["n"],
        independent_first_refusal_net_gain=ind["candidate"]["first"]["correct_refusals"]["n"]
        > ind["baseline"]["first"]["correct_refusals"]["n"],
        no_paired_first_or_final_strict_losses=not any(
            g[s]["losses"] for g in groups for s in STAGES
        ),
        no_supported_answer_to_refusal=not any(
            g[s]["supported_answer_to_refusal"] for g in groups for s in STAGES
        ),
        independent_supported_answerables_preserved=all(
            ind["candidate"][s]["supported_answerable"]
            >= ind["baseline"][s]["supported_answerable"]
            for s in STAGES
        ),
        historical_faults_all_repaired=all(
            r["first"]["strict"] and r["first"]["refused"] for r in diagnostic
        ),
        named_positives_first_and_final_strict=all(
            r[s]["strict"] for r in named if r["qid"] in {"B006", "R092"} for s in STAGES
        ),
        no_extra_corrections=all(
            g["arms"]["candidate"]["corrections"] <= g["arms"]["baseline"]["corrections"]
            for g in groups
        ),
    )
    receipts = {}
    for r in outcomes:
        if r["mode"] != "live":
            continue
        for e in r["events"]:
            val = (e["request_sha256"], receipt_cost(e))
            if receipts.setdefault(e["id"], val) != val:
                raise ValueError("Shared provider ID changed contents or invocation")
    cost = Decimal(money(sum(cost for _, cost in receipts.values())))
    opening = Decimal("3.00814688")
    summary.update(
        gate_checks=checks,
        passes_quality_gate=all(checks.values()),
        actual_live_receipts=len(receipts),
        actual_live_cost_cny=f"{cost:.8f}",
        conservative_cumulative_cny=f"{opening + cost:.8f}",
        remaining_cny=f"{Decimal(30) - opening - cost:.8f}",
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    args = parser.parse_args()
    d = args.directory
    results = lines(d / "outcomes.jsonl")
    rows = rows_from(results, load(d / "grades.json"))
    save(d / "aggregate.json", build(rows, results))
    (d / "row-status.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf8",
    )


if __name__ == "__main__":
    main()
