"""Build aggregate and redacted row results from a completed, fully graded local campaign."""

import argparse
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .meter import CAP, OPENING

STAGES = ("first", "final")
CAPS = (256, 2048)
PAIRED = {"historical", "fresh", "fresh_long", "target"}


def rate(n, d):
    return {"n": n, "denominator": d, "percent": round(100 * n / d, 2) if d else None}


def percentile(numbers, q):
    if not numbers:
        return None
    ordered = sorted(numbers)
    at = (len(ordered) - 1) * q
    left = math.floor(at)
    return round(ordered[left] + (ordered[math.ceil(at)] - ordered[left]) * (at - left), 2)


def money(units):
    return format(Decimal(units) / 100_000_000, ".8f")


def indexed(rows, key):
    result = {key(r): r for r in rows}
    if len(result) != len(rows):
        raise ValueError("Duplicate case, outcome, ledger or grade identity")
    return result


def read_ledger(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute("SELECT count(*) FROM invalid_answers").fetchone()[0]:
            raise ValueError("Confirmed receipt with an unresolved invalid response")
        result = {}
        for r in db.execute("SELECT key,qid,digest,cap,cost,response_id,response FROM calls"):
            if r["cost"] is None or not r["response"] or r["response_id"] in result:
                raise ValueError("Unsettled or duplicate provider receipt")
            result[r["response_id"]] = dict(r)
        return result


def gold_evidence(case):
    required = case["gold"].get("support", [])
    if (
        not case["gold"]["answerable"]
        or not required
        or not all(isinstance(i, str) for i in required)
    ):
        return None
    sent = {s["source_id"] for s in case["selected"]}
    return set(required) <= sent


def gold_passages_fully_visible(case):
    """Check actual transmitted characters for each required new-question source."""
    required = case["gold"].get("support", [])
    if not case["gold"]["answerable"] or not required or not case.get("documents"):
        return None
    documents = {doc["id"]: doc["text"] for doc in case["documents"]}
    for source_id in required:
        source = documents[source_id]
        intervals = []
        for evidence in case["selected"]:
            if evidence["source_id"] != source_id:
                continue
            locator = evidence["locator"]
            start = locator.get("char_start")
            if not isinstance(start, int):
                continue
            end = start + len(evidence["text"])
            if source[start:end] != evidence["text"]:
                continue
            intervals.append((start, end))
        covered = 0
        for start, end in sorted(intervals):
            if start > covered:
                break
            covered = max(covered, end)
        if covered < len(source):
            return False
    return True


def public_grade(grade):
    return {k: grade[k] for k in ("valid", "refused", "value", "support", "language", "strict")}


def parseable(event):
    from packages.knowledge.citation_qa import load_unique_json

    try:
        load_unique_json(event["content"])
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def observations(cases, outcomes, grades, ledger, control_labels):
    case_by = indexed(cases, lambda r: r["id"])
    results = indexed(outcomes, lambda r: (r["id"], r["cap"]))
    marks = indexed(grades, lambda r: (r["id"], r["cap"], r["stage"]))
    expect = {(c["id"], cap) for c in cases for cap in (CAPS if c["cohort"] in PAIRED else (2048,))}
    if expect != set(results) or set(marks) != {(*k, s) for k in expect for s in STAGES}:
        raise ValueError("Missing or surplus registered logical exchange or stage grade")
    seen, rows = set(), []
    for (ident, cap), res in results.items():
        case = case_by[ident]
        qid = case.get("qid", ident)
        cohort = case["cohort"]
        events = res["events"]
        if not 1 <= len(events) <= 2 or events[0]["messages_sha256"] != case["message_sha256"]:
            raise ValueError("Empty, surplus or mismatched physical attempts")
        default_control = any(o["historical_default_control"] for o in case.get("origins", ()))
        default_marks = {
            stage: control_labels.get((qid, stage)) if default_control else None for stage in STAGES
        }
        prior = {}
        for stage in STAGES:
            observed = [
                o["stage_strict"][stage]
                for o in case.get("origins", ())
                if stage in o["stage_strict"]
            ]
            if default_marks[stage] is not None:
                observed.append(default_marks[stage])
            prior[stage] = any(observed) if observed else None
        stages = {}
        for stage in STAGES:
            grade = marks[ident, cap, stage]
            if (grade["qid"], grade["answerable"]) != (qid, case["gold"]["answerable"]):
                raise ValueError("Grade identity or answerability mismatch")
            stages[stage] = public_grade(grade)
        if stages["final"]["valid"] != (res["http"] == 200):
            raise ValueError("Final validity differs from HTTP outcome")
        cost = inp = output = cached = 0
        for attempt, e in enumerate(events, 1):
            receipt = ledger[e["id"]]
            body = json.loads(receipt["response"])
            if (
                e["id"] in seen
                or receipt["key"] != f"{ident}/{cap}/{attempt}"
                or (receipt["qid"], receipt["cap"], receipt["digest"])
                != (qid, cap, e["messages_sha256"])
                or (body["id"], body["created"], body["usage"])
                != (e["id"], e["created"], e["usage"])
                or body["choices"][0]["message"]["content"] != e["content"]
                or body["choices"][0]["finish_reason"] != e["finish"]
            ):
                raise ValueError("Physical call does not reconcile to the endpoint outcome")
            usage = body["usage"]
            prompt, generated = usage["prompt_tokens"], usage["completion_tokens"]
            hit = usage["prompt_tokens_details"]["cached_tokens"]
            hour = datetime.fromtimestamp(body["created"], timezone(timedelta(hours=8))).hour
            expected_cost = ((prompt - hit) * 100 + hit * 2 + generated * 400) * (
                2 if 8 <= hour < 22 else 1
            )
            if (
                not 0 <= hit <= prompt
                or not 0 <= generated <= cap
                or usage["total_tokens"] != prompt + generated
                or expected_cost != receipt["cost"]
            ):
                raise ValueError(
                    "Provider token totals or receipted Beijing tariff do not reconcile"
                )
            seen.add(e["id"])
            cost += receipt["cost"]
            inp += e["usage"]["prompt_tokens"]
            output += e["usage"]["completion_tokens"]
            cached += e["usage"]["prompt_tokens_details"]["cached_tokens"]
        rows.append(
            dict(
                id=ident,
                qid=qid,
                cap=cap,
                cohort=cohort,
                group=case.get("group"),
                historical_arm=case.get("historical_arm"),
                failure_class=case.get("failure_class"),
                protected=case.get("protected", False),
                historical_default_control=any(
                    o["historical_default_control"] for o in case.get("origins", ())
                ),
                historical_strict=prior,
                historical_default_strict=default_marks,
                answerable=case["gold"]["answerable"],
                gold_source_ids_all_visible=gold_evidence(case),
                gold_passages_fully_visible=gold_passages_fully_visible(case),
                http=res["http"],
                first_json_parseable=parseable(events[0]),
                first_finish=events[0]["finish"],
                elapsed_ms=round(res["elapsed_ms"], 3),
                physical=len(events),
                cost_units=cost,
                input_tokens=inp,
                output_tokens=output,
                cached_tokens=cached,
                **stages,
            )
        )
    if set(ledger) != seen:
        raise ValueError("Physical ledger contains a receipt missing from the logical outcomes")
    return rows


def summarize(rows):
    costs = sum(r["cost_units"] for r in rows)
    result = dict(
        logical=len(rows),
        distinct_questions=len({r["qid"] for r in rows}),
        http_200=rate(sum(r["http"] == 200 for r in rows), len(rows)),
        first_json_parseable=rate(sum(r["first_json_parseable"] for r in rows), len(rows)),
        physical=sum(r["physical"] for r in rows),
        corrections=sum(r["physical"] - 1 for r in rows),
        cost_cny=money(costs),
        latency_ms={
            k: percentile([r["elapsed_ms"] for r in rows], q)
            for k, q in (("p50", 0.5), ("p95", 0.95))
        },
        **{k: sum(r[k] for r in rows) for k in ("input_tokens", "output_tokens", "cached_tokens")},
    )
    for s in STAGES:
        substantive = [r for r in rows if r[s]["valid"] and not r[s]["refused"]]
        negative = [r for r in rows if not r["answerable"]]
        positive = [r for r in rows if r["answerable"]]
        result[s] = {
            key: rate(sum(r[s][key] for r in rows), len(rows))
            for key in ("valid", "strict", "value", "language", "refused")
        }
        result[s].update(
            supported_among_substantive=rate(
                sum(r[s]["support"] for r in substantive), len(substantive)
            ),
            correct_refusals=rate(
                sum(r[s]["strict"] and r[s]["refused"] for r in negative), len(negative)
            ),
            false_refusals=rate(sum(r[s]["refused"] for r in positive), len(positive)),
        )
    return result


def paired(rows):
    idx = {(r["id"], r["cap"]): r for r in rows}
    ids = sorted({r["id"] for r in rows})
    pairs = [(idx[i, 256], idx[i, 2048]) for i in ids]
    metrics = {
        "http_200": lambda r: r["http"] == 200,
        "first_json_parseable": lambda r: r["first_json_parseable"],
    }
    for stage in STAGES:
        metrics[f"{stage}_strict"] = lambda r, s=stage: r[s]["strict"]
        metrics[f"{stage}_correct_refusal"] = lambda r, s=stage: r[s]["strict"] and r[s]["refused"]
        metrics[f"{stage}_supported_substantive"] = lambda r, s=stage: (
            r[s]["valid"] and not r[s]["refused"] and r[s]["support"]
        )
    output = {}
    for name, predicate in metrics.items():
        gains = [b for a, b in pairs if not predicate(a) and predicate(b)]
        losses = [b for a, b in pairs if predicate(a) and not predicate(b)]
        output[name] = dict(
            low_failures_recovered=rate(len(gains), sum(not predicate(a) for a, _ in pairs)),
            lost=len(losses),
            net=len(gains) - len(losses),
            lost_qids=sorted({r["qid"] for r in losses}),
            lost_ids=[r["id"] for r in losses],
        )
    still_502 = [higher for lower, higher in pairs if lower["http"] == 502]
    output["among_current_256_http_502"] = dict(
        rows=len(still_502),
        distinct_questions=len({r["qid"] for r in still_502}),
        higher_http_200=rate(sum(r["http"] == 200 for r in still_502), len(still_502)),
        higher_first_json_parseable=rate(
            sum(r["first_json_parseable"] for r in still_502), len(still_502)
        ),
        higher_first_strict=rate(sum(r["first"]["strict"] for r in still_502), len(still_502)),
        higher_final_strict=rate(sum(r["final"]["strict"] for r in still_502), len(still_502)),
    )
    return output


def build(cases, outcomes, grades, ledger, control_labels=None):
    rows = observations(cases, outcomes, grades, ledger, control_labels or {})
    case_by = {c["id"]: c for c in cases}
    groups = {}
    for name in (
        "historical_empty",
        "historical_truncated",
        "fresh",
        "fresh_long",
        "target",
        "protection",
    ):
        subset = [
            r
            for r in rows
            if (
                ("historical_" + r["failure_class"]) if r["cohort"] == "historical" else r["cohort"]
            )
            == name
        ]
        if not subset:
            continue
        info = dict(
            distinct_questions=len({r["qid"] for r in subset}),
            distinct_messages=len({case_by[r["id"]]["message_sha256"] for r in subset}),
            arms={
                str(cap): summarize([r for r in subset if r["cap"] == cap])
                for cap in sorted({r["cap"] for r in subset})
            },
        )
        if name != "protection":
            info["paired"] = paired(subset)
        eligible = [
            r for r in subset if r["cap"] == 2048 and r["gold_source_ids_all_visible"] is not None
        ]
        if eligible:
            info["answerable_all_gold_sources_visible"] = rate(
                sum(r["gold_source_ids_all_visible"] for r in eligible), len(eligible)
            )
            info["missing_gold_source_qids"] = [
                r["qid"] for r in eligible if not r["gold_source_ids_all_visible"]
            ]
        complete = [r for r in eligible if r["gold_passages_fully_visible"] is not None]
        if complete:
            info["answerable_all_gold_passages_fully_visible"] = rate(
                sum(r["gold_passages_fully_visible"] for r in complete), len(complete)
            )
            info["correctness_by_gold_passage_visibility"] = {
                str(covered): {
                    str(cap): summarize(
                        [
                            r
                            for r in subset
                            if r["cap"] == cap and r["gold_passages_fully_visible"] is covered
                        ]
                    )
                    for cap in CAPS
                }
                for covered in (True, False)
            }
        if name == "target":
            info["by_qid"] = {
                qid: paired([r for r in subset if r["qid"] == qid])
                for qid in sorted({r["qid"] for r in subset})
            }
        groups[name] = info
    protected = [r for r in rows if r["cap"] == 2048 and r["protected"]]
    retention = {}
    for stage in STAGES:
        old = [r for r in protected if r["historical_strict"][stage] is True]
        lost = [r for r in old if not r[stage]["strict"]]
        retention[stage] = dict(
            retained=rate(len(old) - len(lost), len(old)),
            default_control_by_group={
                group: {
                    "known_strict": sum(
                        r["historical_default_strict"][stage] is True
                        for r in protected
                        if r["group"] == group and r["historical_default_control"]
                    ),
                    "known_incorrect": sum(
                        r["historical_default_strict"][stage] is False
                        for r in protected
                        if r["group"] == group and r["historical_default_control"]
                    ),
                    "unscored": sum(
                        r["historical_default_strict"][stage] is None
                        for r in protected
                        if r["group"] == group and r["historical_default_control"]
                    ),
                    "lost_qids": sorted(
                        {
                            r["qid"]
                            for r in lost
                            if r["group"] == group and r["historical_default_strict"][stage] is True
                        }
                    ),
                }
                for group in sorted(
                    {r["group"] for r in protected if r["historical_default_control"]}
                )
            },
            historically_unscored=sum(r["historical_strict"][stage] is None for r in protected),
            historically_scored_incorrect=sum(
                r["historical_strict"][stage] is False for r in protected
            ),
            previously_correct_refusal_losses=[r["id"] for r in lost if not r["answerable"]],
            previously_supported_answer_losses=[r["id"] for r in lost if r["answerable"]],
            lost_ids=[r["id"] for r in lost],
            lost_qids=sorted({r["qid"] for r in lost}),
        )
    fresh = [g for g in ("fresh", "fresh_long") if g in groups]
    enough = len(groups) == 6 and len(fresh) == 2
    gates = {
        "all_registered_cohorts_observed": enough,
        "both_failure_classes_gain_final_structure": all(
            groups.get(g, {}).get("paired", {}).get("http_200", {}).get("net", 0) > 0
            for g in ("historical_empty", "historical_truncated")
        ),
        "independent_net_strict_gain_at_first_or_final": bool(fresh)
        and any(sum(groups[g]["paired"][s + "_strict"]["net"] for g in fresh) > 0 for s in STAGES),
        "no_independent_strict_losses": all(
            not groups[g]["paired"][s + "_strict"]["lost"] for g in fresh for s in STAGES
        ),
        "no_independent_refusal_or_support_losses": all(
            not groups[g]["paired"][s + "_" + k]["lost"]
            for g in fresh
            for s in STAGES
            for k in ("correct_refusal", "supported_substantive")
        ),
        "no_paired_correct_refusal_or_supported_answer_losses": all(
            not info["paired"][s + "_" + k]["lost"]
            for info in groups.values()
            if "paired" in info
            for s in STAGES
            for k in ("correct_refusal", "supported_substantive")
        ),
        "no_archived_strict_losses": all(not entry["lost_ids"] for entry in retention.values()),
    }
    total = sum(r["cost_units"] for r in rows)
    if OPENING + total > CAP:
        raise ValueError("Campaign exceeded its registered cumulative budget")
    return dict(
        groups=groups,
        protected_stages=retention,
        gate_checks=gates,
        passes_registered_gate=all(gates.values()),
        by_cap={str(c): summarize([r for r in rows if r["cap"] == c]) for c in CAPS},
        ledger=dict(
            physical=len(ledger),
            cost_cny=money(total),
            opening_cny=money(OPENING),
            cumulative_with_opening_cny=money(total + OPENING),
            remaining_cny=money(CAP - total - OPENING),
        ),
    ), rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--directory", type=Path, default=Path(".scratch/output-limit-20261119"))
    ap.add_argument("--public-rows", type=Path)
    args = ap.parse_args()
    d = args.directory
    cases = json.loads((d / "cases.json").read_bytes())
    outcomes = [json.loads(line) for line in (d / "outcomes.jsonl").read_bytes().splitlines()]
    grades = json.loads((d / "grades.json").read_bytes())
    from .cases import digest, history_control_labels

    freeze = json.loads((d / "freeze.json").read_bytes())
    if digest(cases) != freeze["inputs"] or [
        (p["id"], p["cap"], p["wire"]) for p in freeze["pairs"]
    ] != [(r["id"], r["cap"], r["events"][0]["request_sha256"]) for r in outcomes]:
        raise ValueError("Cases or physical outcomes differ from the complete frozen schedule")
    report, rows = build(
        cases,
        outcomes,
        grades,
        read_ledger(d / "ledger.sqlite"),
        history_control_labels(Path(__file__).resolve().parents[4]),
    )
    (d / "aggregate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    output = args.public_rows or d / "row-status.jsonl"
    output.write_text(
        "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf8",
    )


if __name__ == "__main__":
    main()
