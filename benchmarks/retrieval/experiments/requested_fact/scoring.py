"""Blind, paired scoring of the production requested-fact contract at 2,048 tokens."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from benchmarks.retrieval.experiments.first_refusal_contract import scoring as first
from benchmarks.retrieval.experiments.first_refusal_contract.report import (
    NAMED,
    rows_from,
)
from benchmarks.retrieval.experiments.output_limit.cases import checked, digest
from benchmarks.retrieval.experiments.output_limit.grading import score as grade
from benchmarks.retrieval.experiments.refusal_prompt import scoring as previous
from packages.core.canonical.json_parse import load_unique_json
from packages.knowledge import citation_qa

ARMS, STAGES = first.ARMS, first.STAGES
BASE = first.ROOT / ".scratch/requested-fact-20261119"
PRIOR_LABELS = "c3694c1a30fd06eebb2ccd811bdabe4440644ef5575f8b3462459836976fff64"
REFUSALS = {
    "Insufficient evidence to answer from the selected knowledge snapshot.": "en",
    "所选知识快照中的证据不足，无法回答这个问题。": "zh",
    "The supplied evidence is insufficient to answer this question.": "en",
    "所给证据不足，无法回答这个问题。": "zh",
}


def packets(cases, outcomes):
    from .endpoint import arm_case

    index = {c["id"]: c for c in cases}
    seen, owners, blind, keys = set(), {}, {}, []
    if len(index) != len(cases):
        raise ValueError("Duplicate registered identity")
    for row in outcomes:
        case = index[row["id"]]
        arm = row["arm"]
        key = (case["id"], arm)
        if (
            key in seen
            or arm not in ARMS
            or row["qid"] != case["qid"]
            or row["cohort"] != case["cohort"]
            or row["cap"] != 2048
            or not 1 <= len(row["events"]) <= 2
        ):
            raise ValueError("Invalid endpoint identity, count or output limit")
        seen.add(key)
        msgs = arm_case(case, arm)["messages"]
        wire = case["arm_wires"][arm]
        event = row["events"][0]
        if (
            msgs[1:] != case["messages"][1:]
            or digest(msgs) != event["messages_sha256"]
            or any(
                event[k] != wire[k]
                for k in ("messages_sha256", "request_sha256", "parameters_sha256")
            )
            or case["arm_wires"]["candidate"]["request_sha256"]
            == case["arm_wires"]["baseline"]["request_sha256"]
        ):
            raise ValueError("Unregistered request or differing evidence")
        for e in row["events"]:
            if (
                e["parameters_sha256"] != previous.PARAMETERS
                or owners.setdefault(e["id"], key) != key
            ):
                raise ValueError("Wrong model parameters or receipt reused across cases")
        if len(row["events"]) == 2:
            correction = row["events"][1]
            instructions = (
                (citation_qa._REQUESTED_FACT_CORRECTION,)
                if arm == "candidate"
                else (citation_qa._CITATION_CORRECTION, citation_qa._PROMPT_CITATION_CORRECTION)
            )
            choices = {
                digest(
                    msgs
                    + [
                        {"role": "assistant", "content": event["content"]},
                        {"role": "user", "content": instruction},
                    ]
                )
                for instruction in instructions
            }
            if correction["messages_sha256"] not in choices or row["first"]["valid"]:
                raise ValueError("Correction was not tied to the original invalid return")
        try:
            obj = load_unique_json(event["content"])
        except (ValueError, TypeError, RecursionError):
            obj = {}
        obj = obj if isinstance(obj, dict) else {}
        flag = obj.get("insufficient_evidence")
        if arm == "candidate":
            flag = (
                {"insufficient": True, "supported": False}.get(obj.get("kind"))
                if isinstance(obj.get("kind"), str)
                else None
            )
        for stage in STAGES:
            packet, reason = first.material(case, row, stage)
            uid = digest(packet)
            if packet:
                blind[uid] = dict(uid=uid, **packet)
            keys.append(
                dict(
                    id=case["id"],
                    qid=case["qid"],
                    arm=arm,
                    stage=stage,
                    uid=uid,
                    valid=packet is not None,
                    refused=bool(packet and packet["refused"]),
                    answerable=case["gold"]["answerable"],
                    mode=row["mode"],
                    language_expected=case["language"],
                    reason=reason,
                    http=row["http"],
                    physical_needed=len(row["events"]),
                    raw_model_declared_refusal=flag,
                    raw_cited_refusal=flag is True
                    and bool(
                        obj.get("citation_ids")
                        or re.search(r"\[-?\d+\]", str(obj.get("answer", "")))
                    ),
                    rule=row["first"].get("rule"),
                )
            )
    if seen != {(c["id"], a) for c in cases for a in ARMS}:
        raise ValueError("Missing paired arm")
    return blind, keys


def canonical_label(packet):
    actual = REFUSALS.get(packet["answer"])
    if not actual or not packet["refused"] or packet["cited"]:
        return None
    return dict(
        uid=packet["uid"],
        value=not packet["gold"]["answerable"],
        support=False,
        language=actual == packet["language"],
        reason="Exact uncited refusal, checked against gold answerability and text language.",
    )


def check_history(cases):
    history = [c for c in cases if c["cohort"] == "historical"]
    required = Counter()
    for c in history:
        for stage in STAGES:
            expected = any(o["strict"][stage] for o in c["origins"])
            if expected is not c["prior_strict"][stage]:
                raise ValueError("Stored protection requirement differs from its origins")
            required[stage] += expected
    if (len(history), sum(any(c["prior_strict"].values()) for c in history)) != (446, 441):
        raise ValueError("Incomplete registered historical success population")
    if required != {"first": 394, "final": 441} or sum(c["gold_conflict"] for c in history) != 2:
        raise ValueError("Historical stages or ambiguous-gold cases differ from registration")


def known_labels(blind):
    known = {}
    for path, sha in (
        (first.HISTORY / "judgments.json", first.PRIOR_JUDGMENTS),
        (first.DIRECTORY / "judgments.json", previous.PREVIOUS_JUDGMENTS),
        (previous.ROOT / "development/judgments.json", PRIOR_LABELS),
    ):
        for label in json.loads(checked(path, sha)):
            uid = label["uid"]
            if uid in blind:
                old = known.setdefault(uid, label)
                if any(old[k] != label[k] for k in ("value", "support", "language")):
                    raise ValueError("Historical blind judgments disagree")
    history_count = len(known)
    for uid in sorted(blind.keys() - known.keys()):
        if label := canonical_label(blind[uid]):
            known[uid] = label
    return known, history_count


def review(directory, prepare=False):
    cases = first.load(directory / "cases.json")
    check_history(cases)
    outcomes = first.lines(directory / "outcomes.jsonl")
    blind, keys = packets(cases, outcomes)
    known, historic = known_labels(blind)
    new = [blind[k] for k in sorted(blind.keys() - known.keys())]
    folder = directory / "review"
    if prepare:
        folder.mkdir(exist_ok=True)
        for i in range(3):
            first.save(folder / f"panel-{i}.json", new[i::3])
        return dict(
            stages=len(keys),
            unique=len(blind),
            inherited=historic,
            exact_refusals=len(known) - historic,
            new=len(new),
        )
    labels = [r for i in range(3) for r in first.load(folder / f"labels-{i}.json")]
    revised = [r for p in sorted(folder.glob("adjudicated-*.json")) for r in first.load(p)]
    updates = {r["uid"]: r for r in revised}
    expected = {r["uid"] for r in new}
    if (
        len(updates) != len(revised)
        or updates.keys() - expected
        or {r["uid"] for r in labels} != expected
    ):
        raise ValueError("Unexpected adjudication or incomplete review")
    labels = list(known.values()) + [updates.get(r["uid"], r) for r in labels]
    grades = grade(keys, labels)
    return cases, outcomes, labels, grades


def summarize(cases, rows):
    metadata = {c["id"]: c for c in cases}
    for r in rows:
        c = metadata[r["id"]]
        r.update(cohort=c["cohort"], **{k: c[k] for k in ("false_release", "named", "protected")})
        r["prior_strict"] = c["prior_strict"]
        r["fact_type"] = c.get("fact_type")
        r["gold_conflict"] = c.get("gold_conflict", False)
    fresh = [r for r in rows if r["cohort"] == "fresh"]
    strata = Counter((r["language"], r["answerable"]) for r in fresh if r["arm"] == "baseline")
    if strata != {(lang, b): 12 for lang in ("en", "zh") for b in (True, False)}:
        raise ValueError("Bilingual independent strata differ from registration")
    named = [r for r in rows if r["named"] and r["arm"] == "candidate"]
    releases = [r for r in rows if r["false_release"] and r["arm"] == "candidate"]
    if (
        Counter(r["qid"] for r in named) != Counter([*NAMED, "W13"])
        or len(releases) != 17
        or any(r["answerable"] for r in releases)
    ):
        raise ValueError("Named or prior false-release cohort changed")
    groups, gate = {}, {}
    subsets = {
        "fresh": fresh,
        "historical": [r for r in rows if r["cohort"] == "historical"],
        "named": [r for r in rows if r["named"]],
        "false_release": [r for r in rows if r["false_release"]],
        "protected": [r for r in rows if r["protected"]],
    }
    for lang in ("en", "zh"):
        subsets["fresh_" + lang] = [r for r in fresh if r["language"] == lang]
    for kind in sorted({metadata[r["id"]]["fact_type"] for r in fresh}):
        subsets["fresh_" + kind] = [r for r in fresh if metadata[r["id"]]["fact_type"] == kind]
    for group, subset in subsets.items():
        groups[group], checks = previous.quality_checks(subset)
        if group in ("fresh", "historical"):
            gate.update({group + "_" + k: v for k, v in checks.items()})
    b, c = (groups["fresh"]["arms"][a]["first"] for a in ARMS)
    for k in ("strict", "correct_refusals"):
        gate["independent_net_gain_" + k] = c[k]["n"] > b[k]["n"]
    gate["named_first_and_final_correct"] = all(r[s]["strict"] for r in named for s in STAGES)
    gate["prior_false_releases_reduced"] = all(
        sum(r[s]["valid"] and not r[s]["refused"] for r in releases) < 17
        and any(r[s]["strict"] and r[s]["refused"] for r in releases)
        for s in STAGES
    )
    target_arms = groups["false_release"]["arms"]
    for s in STAGES:
        old_count = target_arms["baseline"][s]["unanswerable_substantive_answers"]
        new_count = target_arms["candidate"][s]["unanswerable_substantive_answers"]
        gate["paired_false_releases_reduced_" + s] = (
            new_count < old_count if old_count else new_count == 0
        )
    history = [r for r in rows if r["cohort"] == "historical" and r["arm"] == "candidate"]
    union = {}
    for s in STAGES:
        protected = [r for r in history if r["prior_strict"][s]]
        union[s] = dict(
            required=len(protected), losses=[r["id"] for r in protected if not r[s]["strict"]]
        )
    gate["historical_union_preserved"] = all(not union[s]["losses"] for s in STAGES)
    return groups, union, gate


def main():
    from .campaign import reconcile

    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action", choices=("prepare", "score"))
    cli.add_argument("--directory", type=Path, default=BASE)
    args = cli.parse_args()
    d = args.directory
    account = reconcile(d)
    if args.action == "prepare":
        print(json.dumps(review(d, prepare=True)))
        return
    cases, outcomes, labels, grades = review(d)
    rows = rows_from(outcomes, grades)
    for r, obs in zip(rows, outcomes, strict=True):
        r["source"] = obs["source"]
    groups, union, gate = summarize(cases, rows)
    result = dict(
        groups=groups,
        historical_union=union,
        gate_checks=gate,
        eligible_for_promotion=all(gate.values()),
        account=account,
    )
    first.save(d / "judgments.json", labels)
    first.save(d / "grades.json", grades)
    first.save(d / "aggregate.json", result)
    (d / "row-status.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8"
    )
    print(json.dumps(gate))


if __name__ == "__main__":
    main()
