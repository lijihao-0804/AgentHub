"""Blind semantic review and paired promotion decisions for the prompt-only trial."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from benchmarks.retrieval.experiments.first_refusal_contract import scoring as prior
from benchmarks.retrieval.experiments.first_refusal_contract.report import (
    FAILURES,
    NAMED,
    compare,
    rows_from,
)
from benchmarks.retrieval.experiments.output_limit.cases import checked, digest
from benchmarks.retrieval.experiments.output_limit.endpoint import MODEL, frozen_retrieval
from benchmarks.retrieval.experiments.output_limit.grading import score as grade_stages
from benchmarks.retrieval.experiments.output_limit.report import read_ledger
from packages.core.canonical.json_parse import load_unique_json
from packages.knowledge import citation_qa

from . import billing
from .endpoint import arm_case

ARMS, STAGES = prior.ARMS, prior.STAGES
ROOT = prior.ROOT / ".scratch/refusal-prompt-20261119"
PREVIOUS_JUDGMENTS = "95dc2d1242a5b1139c1bfeae02d7cccd8250240a198c2e976c903f5d1a718475"
PARAMETERS = digest({"model": MODEL, "max_tokens": 2048, "temperature": 0.0})


def packets(cases, outcomes):
    index = {case["id"]: case for case in cases}
    if len(index) != len(cases):
        raise ValueError("Duplicate registered identity")
    for case in cases:
        frozen_retrieval(case, case["snapshot"])
        if len({digest(arm_case(case, a)["messages"]) for a in ARMS}) != 2:
            raise ValueError("Both arms have the same system prompt")
    seen, blind, keys, owners = set(), {}, [], {}
    for row in outcomes:
        arm = row["arm"]
        case = index[row["id"]]
        ident = (case["id"], arm)
        if (
            arm not in ARMS
            or ident in seen
            or row["qid"] != case["qid"]
            or row["cohort"] != case["cohort"]
            or row["cap"] != 2048
            or not 1 <= len(row["events"]) <= 2
        ):
            raise ValueError("Unexpected endpoint identity or attempt count")
        seen.add(ident)
        msgs = arm_case(case, arm)["messages"]
        wire = case["arm_wires"][arm]
        first = row["events"][0]
        if digest(msgs) != first["messages_sha256"] or any(
            wire[k] != first[k] for k in ("parameters_sha256", "messages_sha256", "request_sha256")
        ):
            raise ValueError("First response does not belong to the registered treatment")
        for e in row["events"]:
            if e["parameters_sha256"] != PARAMETERS or owners.setdefault(e["id"], arm) != arm:
                raise ValueError("Invalid parameters or cross-arm provider receipt reuse")
        if len(row["events"]) == 2:
            correction = row["events"][1]
            expected = {
                digest(
                    msgs
                    + [
                        {"role": "assistant", "content": first["content"]},
                        {"role": "user", "content": message},
                    ]
                )
                for message in (
                    citation_qa._CITATION_CORRECTION,
                    citation_qa._PROMPT_CITATION_CORRECTION,
                )
            }
            if correction["messages_sha256"] not in expected or row["first"]["valid"]:
                raise ValueError("Correction does not follow the original failed response")
        try:
            obj = load_unique_json(first["content"])
        except (TypeError, ValueError, RecursionError):
            obj = {}
        obj = obj if isinstance(obj, dict) else {}
        flag = obj.get("insufficient_evidence")

        cited_flag = flag is True and bool(
            obj.get("citation_ids") or re.search(r"\[-?\d+\]", str(obj.get("answer", "")))
        )
        for stage in STAGES:
            packet, error = prior.material(case, row, stage)
            uid = digest(packet)
            if packet:
                blind[uid] = dict(uid=uid, **packet)
            keys.append(
                dict(
                    id=row["id"],
                    qid=row["qid"],
                    arm=arm,
                    stage=stage,
                    uid=uid,
                    valid=packet is not None,
                    refused=bool(packet and packet["refused"]),
                    answerable=case["gold"]["answerable"],
                    language_expected=case["language"],
                    mode=row["mode"],
                    reason=error,
                    http=row["http"],
                    physical_needed=len(row["events"]),
                    raw_model_declared_refusal=flag,
                    raw_cited_refusal=cited_flag,
                    rule=row["first"].get("rule"),
                )
            )
    if seen != {(i, a) for i in index for a in ARMS}:
        raise ValueError("Incomplete treatment coverage")
    return blind, keys


def known_and_new(blind, directory):
    sources = [
        json.loads(checked(prior.HISTORY / "judgments.json", prior.PRIOR_JUDGMENTS)),
        json.loads(checked(prior.DIRECTORY / "judgments.json", PREVIOUS_JUDGMENTS)),
    ]
    if directory.name == "protection":
        sources.append(prior.load(directory.parent / "development/judgments.json"))
    known = {}
    for labels in sources:
        for row in labels:
            uid = row["uid"]
            if uid not in blind:
                continue
            old = known.setdefault(uid, row)
            if any(old[k] != row[k] for k in ("value", "support", "language")):
                raise ValueError("Conflicting historical blind judgments")
    return list(known.values()), [blind[k] for k in sorted(blind.keys() - known.keys())]


def review(directory, prepare=False):
    cases = prior.load(directory / "cases.json")
    outcomes = prior.lines(directory / "outcomes.jsonl")
    blind, keys = packets(cases, outcomes)
    known, pending = known_and_new(blind, directory)
    folder = directory / "review"
    if prepare:
        folder.mkdir(exist_ok=True)
        for i in range(3):
            prior.save(folder / f"panel-{i}.json", pending[i::3])
        return dict(stages=len(keys), unique=len(blind), inherited=len(known), pending=len(pending))
    reviewed = [r for p in sorted(folder.glob("labels-*.json")) for r in prior.load(p)]
    revised = [r for p in sorted(folder.glob("adjudicated*.json")) for r in prior.load(p)]
    replacements = {r["uid"]: r for r in revised}
    expected = {r["uid"] for r in pending}
    if (
        len(replacements) != len(revised)
        or replacements.keys() - expected
        or {r["uid"] for r in reviewed} != expected
    ):
        raise ValueError("Blind review or adjudication coverage mismatch")
    labels = known + [replacements.get(r["uid"], r) for r in reviewed]
    return cases, outcomes, labels, grade_stages(keys, labels)


def nondecreasing_rate(old, new):
    # If the control supplied no substantive answer, it establishes no floor.
    if old["denominator"] == 0:
        return True
    return bool(
        new["denominator"] and new["n"] * old["denominator"] >= old["n"] * new["denominator"]
    )


def quality_checks(rows):
    delta = compare(rows)
    for arm in ARMS:
        negatives = [r for r in rows if r["arm"] == arm and not r["answerable"]]
        for stage in STAGES:
            delta["arms"][arm][stage]["unanswerable_substantive_answers"] = sum(
                r[stage]["valid"] and not r[stage]["refused"] for r in negatives
            )
    b, c = (delta["arms"][a] for a in ARMS)
    checks = {
        "paired_first_and_final_strict_preserved": all(not delta[s]["losses"] for s in STAGES),
        "correct_refusals_preserved": all(not delta[s]["correct_refusal_losses"] for s in STAGES),
        "supported_answers_never_refused": all(
            not delta[s]["supported_answer_to_refusal"] for s in STAGES
        ),
        "supported_answerable_count_preserved": all(
            c[s]["supported_answerable"] >= b[s]["supported_answerable"] for s in STAGES
        ),
        "citation_support_rate_preserved": all(
            nondecreasing_rate(b[s]["citation_support"], c[s]["citation_support"]) for s in STAGES
        ),
        "corrections_nonincreasing": c["corrections"] <= b["corrections"],
    }
    positive = [r for r in rows if r["answerable"]]
    positive_arms = compare(positive)["arms"]
    delta["answerable_arms"] = positive_arms
    by = {(r["id"], r["arm"]): r for r in positive}
    checks["previously_supported_answerables_remain_supported"] = all(
        by[r["id"], "candidate"][s]["valid"]
        and not by[r["id"], "candidate"][s]["refused"]
        and by[r["id"], "candidate"][s]["support"]
        for r in positive
        if r["arm"] == "baseline"
        for s in STAGES
        if r[s]["valid"] and not r[s]["refused"] and r[s]["support"]
    )
    return delta, checks


def summarize(cases, rows, stage):
    by_id = {c["id"]: c for c in cases}
    checks, groups = {}, {}
    for row in rows:
        row["cohort"] = by_id[row["id"]]["cohort"]
        if stage == "protection":
            row["historical_required"] = by_id[row["id"]]["prior_strict"]
    if stage == "development":
        fresh = [r for r in rows if r["cohort"] == "fresh"]
        named = [r for r in rows if r["cohort"] == "target"]
        strata = Counter((r["language"], r["answerable"]) for r in fresh if r["arm"] == "baseline")
        named_candidates = [r for r in named if r["arm"] == "candidate"]
        names = Counter(r["qid"] for r in named_candidates)
        if (
            len(rows) != 110
            or strata != {(lang, a): 12 for lang in ("en", "zh") for a in (True, False)}
            or names != Counter([*NAMED, "W13"])
            or any(r["answerable"] != (r["qid"] not in FAILURES) for r in named_candidates)
        ):
            raise ValueError("Development cohort does not match the registration")
        for group, subset in [("fresh", fresh), ("target", named)]:
            groups[group], guarded = quality_checks(subset)
            checks.update({group + "_" + k: v for k, v in guarded.items()})
        for lang in ("zh", "en"):
            delta, _ = quality_checks([r for r in fresh if r["language"] == lang])
            groups["fresh_" + lang] = delta
            for s in STAGES:
                checks[f"{lang}_{s}_prior_correct_preserved"] = not delta[s]["losses"]
        b, c = (groups["fresh"]["arms"][a]["first"] for a in ARMS)
        for metric in ("strict", "correct_refusals"):
            checks["independent_first_" + metric + "_net_gain"] = c[metric]["n"] > b[metric]["n"]
        checks["five_named_first_refusals_correct"] = all(
            r["first"]["strict"] and r["first"]["refused"]
            for r in named_candidates
            if r["qid"] in FAILURES
        )
        checks["both_named_positives_strict_with_support"] = all(
            r[s]["strict"] and r[s]["support"] and not r[s]["refused"]
            for r in named_candidates
            if r["qid"] not in FAILURES
            for s in STAGES
        )
    elif stage == "protection":
        if (
            len(cases) != 390
            or len({digest(c["messages"][1]) for c in cases}) != 390
            or any(c["cohort"] != "protection" for c in cases)
        ):
            raise ValueError("Historical protection input coverage differs")
        losses = {s: [] for s in STAGES}
        needed = Counter()
        for r in rows:
            if r["arm"] != "candidate":
                continue
            case = by_id[r["id"]]
            for s in STAGES:
                original = any(o["strict"][s] for o in case["origins"])
                if case["prior_strict"][s] is not original:
                    raise ValueError("Frozen union of historical successes differs")
                if original:
                    needed[s] += 1
                    if not r[s]["strict"]:
                        losses[s].append(r["id"])
        groups["protection"], checks = quality_checks(rows)
        groups["historical_union"] = {s: dict(required=needed[s], losses=losses[s]) for s in STAGES}
        checks["historical_correct_stage_union_preserved"] = not any(losses.values())
    else:
        raise ValueError("Unrecognized trial stage")
    return groups, checks


def reconcile(directory, cases, outcomes):
    from .campaign import input_id, physical_plan, plan_history, verify_outcomes

    root = directory.parent
    reg = prior.load(root / "freeze.json")
    if (
        digest(cases) != reg["cases"][directory.name]
        or reg["previous_account"] != billing.ACCOUNT_HASH
    ):
        raise ValueError("Case population or sealed opening account changed")
    old_cases, old_rows, old_grades, opening_ledger = billing.previous()
    targets, guards, history = plan_history(old_cases, old_rows, old_grades)
    target_keys = {input_id(c) for c in targets}
    ledger = read_ledger(root / "ledger.sqlite")
    opening = billing.OPENING
    development_candidate = {}
    stages = ["development"] if directory.name == "development" else ["development", "protection"]
    for stage in stages:
        where = root / stage
        observed = outcomes if stage == directory.name else prior.lines(where / "outcomes.jsonl")
        inputs = cases if stage == directory.name else prior.load(where / "cases.json")
        if digest(inputs) != reg["cases"][stage]:
            raise ValueError("Stage input file digest mismatch")
        if stage == "protection":
            expected = {c["id"]: (c["origins"], c["prior_strict"]) for c in guards}
            actual = {c["id"]: (c["origins"], c["prior_strict"]) for c in inputs}
            if actual != expected:
                raise ValueError(
                    "Historical protection union differs from the sealed earlier grades"
                )
        verify_outcomes(
            observed, physical_plan(inputs, target_keys), history, development_candidate
        )
        new_ids = {e["id"] for r in observed if r["source"] == "live" for e in r["events"]}
        snapshot = {k: ledger[k] for k in opening_ledger.keys() | new_ids}
        if stage == "protection" and snapshot.keys() != ledger.keys():
            raise ValueError("Unattributed paid receipt after protection")
        account = billing.close(observed, opening_ledger, snapshot, opening)
        account["freeze_sha"] = digest(reg)
        if account != prior.load(where / "account.json"):
            raise ValueError("Stage settlement does not reconcile")
        if stage == "development":
            if (
                snapshot.keys() != ledger.keys()
                and not (root / "protection/dispatch.intent").exists()
            ):
                raise ValueError("Unattributed paid receipt after development")
            by = {c["id"]: c for c in inputs}
            development_candidate = {
                input_id(by[r["id"]]): r
                for r in observed
                if r["arm"] == "candidate" and r["cohort"] == "target"
            }
        opening_ledger = snapshot
        opening += account["new_cost_units"]
    return account


def report(directory):
    cases, outcomes, labels, grades = review(directory)
    if (
        prior.load(directory / "judgments.json") != labels
        or prior.load(directory / "grades.json") != grades
    ):
        raise ValueError("Saved grades differ from recomputed blind review")
    account = reconcile(directory, cases, outcomes)
    rows = rows_from(outcomes, grades)
    for row, observed in zip(rows, outcomes, strict=True):
        row["source"] = observed["source"]
    groups, checks = summarize(cases, rows, directory.name)
    checks["closed_account_reconciled"] = True
    if directory.name == "protection":
        checks["development_gate_passed"] = all(
            development_gate(directory.parent / "development").values()
        )
    result = dict(
        stage=directory.name,
        groups=groups,
        gate_checks=checks,
        passes_stage=all(checks.values()),
        eligible_for_promotion=directory.name == "protection" and all(checks.values()),
        account=account,
    )
    return result, rows


def development_gate(directory):
    directory = Path(directory)
    if directory.name != "development":
        raise ValueError("Expected registered development stage directory")
    return report(directory)[0]["gate_checks"]


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=("prepare", "score", "report", "summary"))
    cli.add_argument("--directory", type=Path, default=ROOT / "development")
    args = cli.parse_args()
    directory = args.directory
    if args.action == "prepare":
        print(json.dumps(review(directory, prepare=True)))
    elif args.action == "score":
        _, _, labels, grades = review(directory)
        prior.save(directory / "judgments.json", labels)
        prior.save(directory / "grades.json", grades)
        print(json.dumps(dict(stages=len(grades), unique=len(labels))))
    else:
        result, rows = report(directory)
        prior.save(directory / "aggregate.json", result)
        (directory / "row-status.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8"
        )
        print(json.dumps(result["gate_checks"]))


if __name__ == "__main__":
    main()
