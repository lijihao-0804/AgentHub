"""Freeze, run and reconcile the two-stage prompt-only QA trial."""

import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path

from benchmarks.retrieval.table_header_answers import write_entry
from scripts.aliyun_api_smoke import aliyun_route

from ..first_refusal_contract import campaign as prior
from ..first_refusal_contract.endpoint import ARMS, PairedTransport
from ..output_limit.cases import digest, normalized
from ..output_limit.meter import BLOCKED_MESSAGES, BLOCKED_QIDS
from ..output_limit.report import read_ledger
from ..output_limit.tail_recovery import lines, load, persist
from .billing import ACCOUNT_HASH, OPENING, StageMeter, close, previous
from .endpoint import PromptEndpoint

OUT = prior.ROOT / ".scratch/refusal-prompt-20261119"
FOLDER = Path(__file__).resolve().parent
STAGES = ("development", "protection")


def source_control():
    # Bind current committed code, source fixtures and both earlier harnesses.
    # Never compare the current candidate product against an old campaign HEAD.
    roots = [
        "apps/api",
        "packages",
        "scripts/aliyun_api_smoke.py",
        "benchmarks/retrieval/table_header_answers.py",
        "benchmarks/public_rag/model-route.json",
        *(
            str((FOLDER.parent / p).relative_to(prior.ROOT).as_posix())
            for p in ("output_limit", "first_refusal_contract", "refusal_prompt")
        ),
    ]
    if prior.git("rev-parse", "origin/main") != prior.MAIN:
        raise ValueError("Current main differs from the registered baseline")
    suffix = (".py", "protocol.md", "fixtures/fresh.jsonl", "model-route.json")
    untracked = prior.git("ls-files", "--others", "--exclude-standard", "--", *roots).splitlines()
    if any(p.endswith(suffix) for p in untracked):
        raise ValueError("Uncommitted runtime, input fixture, or protocol")
    paths = {p for p in prior.git("ls-files", "--", *roots).splitlines() if p.endswith(suffix)}
    if set(prior.git("diff", "--name-only", "HEAD", "--", *roots).splitlines()) & paths:
        raise ValueError("Registered source differs from the working tree")
    tree = prior.git("ls-tree", "-r", "HEAD", "--", *roots).splitlines()
    return dict(
        commit=prior.git("rev-parse", "HEAD"),
        main=prior.MAIN,
        tree=digest([r for r in tree if r.rsplit("\t", 1)[-1] in paths]),
    )


def input_id(c):
    return digest([c["question"], c["messages"][1]])


def plan_history(cases, outcomes, grades):
    baseline = {r["id"]: r for r in outcomes if r["arm"] == "baseline"}
    flags = {(g["id"], g["arm"], g["stage"]): g["strict"] for g in grades}
    groups, targets = {}, []
    for c in cases:
        ident = c["id"]
        strict = {s: any(flags[ident, a, s] for a in ARMS) for s in ("first", "final")}
        if c["cohort"] == "target":
            targets.append(
                dict(c, id="development/" + ident, ref_id=ident, mode="live", cohort="target")
            )
            continue
        if c["mode"] != "archive" and not (c["cohort"] == "fresh" and any(strict.values())):
            continue
        groups.setdefault(input_id(c), []).append((c, strict))
    guards = []
    for key, group in sorted(groups.items()):
        # Prefer the single archived baseline that demonstrates the most correct
        # stages; retain the complete union even if different samples carry them.
        def rank(pair):
            c, _ = pair
            return (-sum(flags[c["id"], "baseline", s] for s in ("first", "final")), c["id"])

        source = min(group, key=rank)[0]
        origins = [
            dict(id=c["id"], strict=st, baseline_receipt=baseline[c["id"]]["events"][0]["id"])
            for c, st in group
        ]
        if any(
            c["gold"] != source["gold"]
            or c["qid"] != source["qid"]
            or c["selected"] != source["selected"]
            for c, _ in group
        ):
            raise ValueError("Conflicting evidence metadata for duplicate input")
        guards.append(
            dict(
                source,
                id="protection/" + key,
                mode="live",
                cohort="protection",
                ref_id=source["id"],
                origins=origins,
                prior_strict={s: any(st[s] for _, st in group) for s in ("first", "final")},
            )
        )
    if (
        len(guards) != 390
        or len(targets) != 7
        or len({input_id(c) for c in targets} & set(groups)) != 7
    ):
        raise ValueError("Historical coverage differs from sealed campaign")
    return targets, guards, baseline


def fresh_cases(previous_cases):
    records = load(OUT / "independent/fresh-cases.json")
    fixtures = {r["id"]: r for r in lines(FOLDER / "fixtures/fresh.jsonl")}
    questions = [normalized(c["question"]) for c in records]
    if (
        len(records) != 48
        or len(set(questions)) != 48
        or set(questions) & {normalized(c["question"]) for c in previous_cases}
        or Counter((c["language"], c["gold"]["answerable"]) for c in records)
        != {(lang, g): 12 for lang in ("en", "zh") for g in (False, True)}
        or set(fixtures) != {c["id"] for c in records}
    ):
        raise ValueError("Incorrect independent strata or reused question")
    output = []
    for c in records:
        if (
            any(c[k] != v for k, v in fixtures[c["id"]].items())
            or not c.get("kb")
            or not c.get("snapshot")
        ):
            raise ValueError("Unbound independent case")
        output.append(
            dict(
                c,
                id="development/fresh/" + c["id"],
                qid=c["id"],
                cohort="fresh",
                mode="live",
                ref_id=None,
            )
        )
    return output


def physical_plan(cases, target_keys):
    # Every fresh arm is paid. A prior baseline and a candidate already observed
    # in this development stage can replay only the identical physical wires.
    return [
        (
            c,
            a,
            (
                "historical_baseline"
                if a == "baseline" and c["ref_id"]
                else "development_candidate_replay"
                if a == "candidate" and c["cohort"] == "protection" and input_id(c) in target_keys
                else "live"
            ),
        )
        for i, c in enumerate(cases)
        for a in prior.arm_order(i)
    ]


async def freeze(cases, previous_rows, ledger, stage):
    transport = PairedTransport(ledger)
    async with AsyncExitStack() as stack:
        endpoints = {
            a: await stack.enter_async_context(PromptEndpoint(aliyun_route(), a)) for a in ARMS
        }
        for c in cases:
            wires = {}
            for a in ARMS:
                ep = endpoints[a]
                replay = a == "baseline" and c["ref_id"] is not None
                if replay:
                    ep.meter = transport
                    row = previous_rows[c["ref_id"]]
                    local = dict(c, mode="archive")
                    transport.select(local, row)
                    obs = await ep.measure(local)
                    if not prior.historical_match(obs, row):
                        raise ValueError("Main replay changed its historical baseline")
                else:
                    ep.meter = None
                    obs = await ep.measure(c)
                    if obs["http"] != 200 or len(obs["events"]) != 1:
                        raise ValueError("First physical dry run failed")
                ev = obs["events"][0]
                wires[a] = {
                    k: ev[k] for k in ("request_sha256", "messages_sha256", "parameters_sha256")
                }
                if c["qid"] in BLOCKED_QIDS or wires[a]["messages_sha256"] in BLOCKED_MESSAGES:
                    raise ValueError("Historically unresolved case")
            if (
                wires["baseline"]["request_sha256"] == wires["candidate"]["request_sha256"]
                or wires["baseline"]["parameters_sha256"] != wires["candidate"]["parameters_sha256"]
                or wires["baseline"]["messages_sha256"] != digest(c["messages"])
            ):
                raise ValueError("Prompt-only wire attribution mismatch")
            c["arm_wires"] = wires
    folder = OUT / stage
    folder.mkdir(exist_ok=True, parents=True)
    persist(folder / "cases.json", cases)


def read_registered(stage, reg):
    cases = load(OUT / stage / "cases.json")
    if digest(cases) != reg["cases"][stage]:
        raise ValueError("Registered cases changed")
    return cases


def verify_outcomes(rows, plan, historical, paid_prior):
    if [(r["id"], r["arm"], r["source"]) for r in rows] != [
        (c["id"], a, src) for c, a, src in plan
    ]:
        raise ValueError("Incomplete stage or changed schedule")
    for obs, (c, a, src) in zip(rows, plan, strict=True):
        ev = obs["events"][0]
        if (
            any(ev[k] != v for k, v in c["arm_wires"][a].items())
            or obs["qid"] != c["qid"]
            or obs["cohort"] != c["cohort"]
            or obs["mode"] != ("archive" if src == "historical_baseline" else "live")
        ):
            raise ValueError("Outcome identity or physical wire mismatch")
        if src == "live":
            if any(e["provenance"] != "provider" for e in obs["events"]):
                raise ValueError("A new wire lacks its own physical provider response")
        else:
            archive = (
                historical[c["ref_id"]] if src == "historical_baseline" else paid_prior[input_id(c)]
            )
            if not prior.historical_match(obs, archive) or obs["first"] != archive["first"]:
                raise ValueError("Archived baseline or prior-stage candidate changed")


def stage_material(stage, reg, history, ledger):
    development = read_registered("development", reg)
    target_keys = {c["id"]: input_id(c) for c in development if c["cohort"] == "target"}
    prior_stage = {}
    opening = OPENING
    gate_material = None
    if stage == "protection":
        d = OUT / "development"
        dev_rows = lines(d / "outcomes.jsonl")
        plan = physical_plan(development, set(target_keys.values()))
        verify_outcomes(dev_rows, plan, history, {})
        # This read is restricted to the registered development keys, so stage
        # two can still be sealed after it appends to the same durable ledger.
        current = read_ledger(OUT / "ledger.sqlite")
        dev_ids = {e["id"] for r in dev_rows if r["source"] == "live" for e in r["events"]}
        prior_ledger = {i: current[i] for i in ledger.keys() | dev_ids}
        dev_account = close(dev_rows, ledger, prior_ledger, OPENING)
        dev_account["freeze_sha"] = digest(reg)
        if dev_account != load(d / "account.json"):
            raise ValueError("Development account not closed against the current ledger")
        ledger = prior_ledger
        opening += dev_account["new_cost_units"]
        from .scoring import development_gate

        checks = development_gate(d)
        if (
            not isinstance(checks, dict)
            or not checks
            or not all(v is True for v in checks.values())
        ):
            raise ValueError("Development quality gate did not pass")
        gate_material = dict(
            grades_sha=digest(load(d / "grades.json")),
            account_sha=digest(dev_account),
            checks=checks,
        )
        for r in dev_rows:
            if r["arm"] == "candidate" and r["id"] in target_keys:
                prior_stage[target_keys[r["id"]]] = r
        if len(prior_stage) != 7:
            raise ValueError("Seven paid candidate receipts are required")
    cases = read_registered(stage, reg)
    return (
        physical_plan(cases, set(target_keys.values())),
        ledger,
        opening,
        prior_stage,
        gate_material,
    )


async def run(args):
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    cases, outputs, grades, ledger = previous()
    targets, guards, history = plan_history(cases, outputs, grades)
    if args.action == "check-history":
        print(
            json.dumps(
                {
                    "prior_ids": 1760,
                    "opening_cny": "3.12094384",
                    "guards": len(guards),
                    "development_max_calls": 206,
                    "protection_max_calls": 766,
                    "external_calls": 0,
                }
            )
        )
        return
    control = source_control()
    if args.action == "freeze":
        dev = fresh_cases(cases) + targets
        if len({input_id(c) for c in dev}) != len(dev):
            raise ValueError("Duplicate development case")
        await freeze(dev, history, ledger, "development")
        await freeze(guards, history, ledger, "protection")
        reg = dict(
            source=control,
            previous_account=ACCOUNT_HASH,
            cases={s: digest(lst) for s, lst in zip(STAGES, (dev, guards), strict=True)},
            physical_upper_bounds=dict(development=206, protection=766),
        )
        persist(OUT / "freeze.json", reg)
        print(json.dumps({"freeze_sha": digest(reg), "external_calls": 0}))
        return
    reg = load(OUT / "freeze.json")
    if (
        args.freeze_sha != digest(reg)
        or reg["previous_account"] != ACCOUNT_HASH
        or control["tree"] != reg["source"]["tree"]
        or control["main"] != reg["source"]["main"]
    ):
        raise ValueError("Source or registration mismatch")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", reg["source"]["commit"], control["commit"]],
        cwd=prior.ROOT,
        check=True,
    )
    stage = args.stage
    plan, inherited, opening, paid_prior, gate_material = stage_material(
        stage, reg, history, ledger
    )
    d = OUT / stage
    dest = OUT / "ledger.sqlite"
    stage_intent = dict(freeze_sha=digest(reg), stage=stage, development_gate=gate_material)
    if args.action == "seal":
        if load(d / "dispatch.intent") != stage_intent:
            raise ValueError("Stage intent or development grade inputs changed")
        rows = lines(d / "outcomes.jsonl")
        verify_outcomes(rows, plan, history, paid_prior)
        report = close(rows, inherited, read_ledger(dest), opening)
        report["freeze_sha"] = digest(reg)
        persist(d / "account.json", report)
        print(json.dumps(report))
        return
    # A one-shot stage intent survives interruption. No UNKNOWN may be retried.
    with (d / "dispatch.intent").open("x", encoding="ascii") as intent:
        json.dump(stage_intent, intent, ensure_ascii=True, sort_keys=True)
        intent.flush()
        os.fsync(intent.fileno())
    meter = StageMeter(dest, inherited, create=stage == "development")
    wire = PairedTransport(inherited, meter)
    async with AsyncExitStack() as stack:
        eps = {
            a: await stack.enter_async_context(PromptEndpoint(aliyun_route(), a, wire))
            for a in ARMS
        }
        with (d / "outcomes.jsonl").open("x", encoding="utf8") as stream:
            for c, a, src in plan:
                replays = src != "live"
                local = dict(c, mode="archive" if replays else "live")
                prior_row = None
                if replays:
                    prior_row = (
                        history[c["ref_id"]]
                        if src == "historical_baseline"
                        else paid_prior[input_id(c)]
                    )
                wire.select(local, prior_row)
                obs = await eps[a].measure(local, c["arm_wires"][a]["request_sha256"])
                obs["source"] = src
                if src == "development_candidate_replay":
                    obs["mode"] = "live"
                    for event in obs["events"]:
                        event["provenance"] = src
                verify_outcomes([obs], [(c, a, src)], history, paid_prior)
                write_entry(stream, obs)
                print(stage, c["id"], a, src, obs["http"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check-history", "freeze", "dispatch", "seal"))
    parser.add_argument("--stage", choices=STAGES, default="development")
    parser.add_argument("--freeze-sha")
    asyncio.run(run(parser.parse_args()))
