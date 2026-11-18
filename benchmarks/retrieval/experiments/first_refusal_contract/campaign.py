"""Run the registered 2048-token pair. Only dispatch may contact a provider."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path

from benchmarks.retrieval.table_header_answers import write_entry
from scripts.aliyun_api_smoke import aliyun_route

from ..output_limit.cases import digest, normalized
from ..output_limit.meter import BLOCKED_MESSAGES, BLOCKED_QIDS
from ..output_limit.report import read_ledger
from ..output_limit.tail_recovery import DIRECTORY as PREVIOUS
from ..output_limit.tail_recovery import ROOT, lines, load, persist
from .billing import CARRIED, EARLY_HASH, TAIL_HASH, SuccessorMeter, account, history
from .endpoint import ARMS, PairedEndpoint, PairedTransport

FOLDER = Path(__file__).resolve().parent
OUT = ROOT / ".scratch/first-refusal-20261119"
MAIN = "deb28ec77fa85a9cf2c888dd46d59a8360efa189"
TARGETS = {"N035", "W13", "2hop__8556_47169", "B097", "R092", "B006"}
MATERIAL = "e5300ba6d78fbd76a9c9bd423649eed79e0f99fe"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()


def control():
    folder = FOLDER.relative_to(ROOT).as_posix()
    roots = [
        "packages",
        "apps/api",
        folder,
        "benchmarks/retrieval/experiments/output_limit",
        "scripts/aliyun_api_smoke.py",
        "benchmarks/retrieval/table_header_answers.py",
        "benchmarks/public_rag/model-route.json",
    ]
    tracked = git("ls-files", "--", *roots).splitlines()
    missing = git("ls-files", "--others", "--exclude-standard", "--", *roots).splitlines()
    important = [
        p
        for p in tracked
        if p.endswith((".py", "protocol.md", "fixtures/fresh.jsonl", "model-route.json"))
    ]
    if any(p.endswith((".py", "protocol.md", "fixtures/fresh.jsonl")) for p in missing):
        raise ValueError("Commit the executable sources and fixture before freezing")
    # Bind tracked executable materials, but permit edits to unrelated reports.
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *roots], cwd=ROOT, check=True)
    if git("rev-parse", "origin/main") != MAIN or git(
        "diff", "--name-only", MAIN, MATERIAL, "--", "apps/api", "packages"
    ):
        raise ValueError("Current-main reference or prior product changed")
    return {
        "main": MAIN,
        "source": git("rev-parse", "HEAD"),
        "material": MATERIAL,
        "tree": digest(
            [
                (p, hashlib.sha256((ROOT / p).read_bytes().replace(b"\r\n", b"\n")).hexdigest())
                for p in important
            ]
        ),
    }


def register_cases(old, fresh=True):
    prior = load(PREVIOUS / "cases.json")
    index = {r["id"]: r for r in prior}
    high = {r["id"]: r for r in old if r["cap"] == 2048}
    labels = lines(FOLDER.parent / "output_limit/results/row-status.jsonl")
    if len(labels) != 511 or {r["id"] for r in labels if r["cap"] == 2048} != set(high):
        raise ValueError("Historical grades incomplete")
    cases, targets = [], {}
    for label in labels:
        if label["cap"] != 2048:
            continue
        ident, qid = label["id"], label["qid"]
        c = index[ident]
        if qid != c.get("qid", ident):
            raise ValueError("Archived case identity mismatch")
        named = qid in TARGETS
        strict = {s: label[s]["strict"] for s in ("first", "final")}
        if not (named or any(strict.values())):
            continue
        wire = high[ident]["events"][0]["request_sha256"]
        common = dict(
            c,
            qid=qid,
            original_id=c.get("original_id", qid),
            archive_id=ident,
            named=named,
            prior_strict=strict,
            registered_wire=wire,
        )
        cases.append(dict(common, id="archive/" + ident, mode="archive", cohort="protection"))
        if named:
            if wire in targets:
                raise ValueError("Unexpected duplicate named wire")
            targets[wire] = dict(common, id="target/" + ident, mode="live", cohort="target")
    if len(cases) != 335 or len(targets) != 7 or {r["qid"] for r in targets.values()} != TARGETS:
        raise ValueError("Historical coverage mismatch")
    cases.extend(targets.values())
    if fresh:
        independent = load(OUT / "independent/fresh-cases.json")
        source = {r["id"]: r for r in lines(FOLDER / "fixtures/fresh.jsonl")}
        old_questions = {normalized(c["question"]) for c in prior}
        fresh_questions = [normalized(c["question"]) for c in independent]
        if (
            Counter((c["language"], c["gold"]["answerable"]) for c in independent)
            != {(lang, gold): 16 for lang in ("en", "zh") for gold in (True, False)}
            or len(set(fresh_questions)) != 64
            or old_questions.intersection(fresh_questions)
            or {c["id"] for c in independent} != set(source)
        ):
            raise ValueError("Independent registration or language strata mismatch")
        for c in independent:
            ident = c["id"]
            if (
                any(c[k] != v for k, v in source[ident].items())
                or not c.get("kb")
                or not c.get("snapshot")
            ):
                raise ValueError("Independent record differs from fixture or lacks KB binding")
            cases.append(
                dict(
                    c,
                    id="fresh/" + ident,
                    qid=ident,
                    cohort="fresh",
                    mode="live",
                    named=False,
                    message_sha256=digest(c["messages"]),
                )
            )
    live = [r for r in cases if r["mode"] == "live"]
    if (
        len({r["id"] for r in cases}) != len(cases)
        or len({r["message_sha256"] for r in live}) != len(live)
        or any(r["qid"] in BLOCKED_QIDS or r["message_sha256"] in BLOCKED_MESSAGES for r in cases)
    ):
        raise ValueError("Blocked or repeated input")
    return cases, high


def arm_order(index):
    return ARMS[::-1] if index % 2 else ARMS


def historical_match(out, prior):
    if out["http"] != prior["http"] or [e["id"] for e in out["events"]] != [
        e["id"] for e in prior["events"]
    ]:
        return False
    if out["http"] == 200:
        return out["body"] == prior["body"]
    return all(out["body"]["error"][k] == prior["body"]["error"][k] for k in ("code", "message"))


async def preflight(cases, high, old):
    transport = PairedTransport(old)
    archive_rows, wires = [], {}
    async with AsyncExitStack() as stack:
        endpoints = {
            a: await stack.enter_async_context(PairedEndpoint(aliyun_route(), a)) for a in ARMS
        }
        for i, c in enumerate(cases):
            for arm in arm_order(i):
                ep = endpoints[arm]
                if c["mode"] == "archive":
                    ep.meter = transport
                    previous = high[c["archive_id"]]
                    transport.select(c, previous)
                    out = await ep.exchange(c)
                    if arm == "baseline" and not historical_match(out, previous):
                        raise ValueError(f"Historical baseline changed: {c['id']}")
                    archive_rows.append(out)
                else:
                    ep.meter = None
                    out = await ep.exchange(c)
                    if out["http"] != 200 or len(out["events"]) != 1:
                        raise ValueError("Inert preflight failed")
                event = out["events"][0]
                first = {
                    k: event[k] for k in ("request_sha256", "messages_sha256", "parameters_sha256")
                }
                if (
                    first["messages_sha256"] != c["message_sha256"]
                    or wires.setdefault(c["id"], first) != first
                ):
                    raise ValueError("Paired first wires differ")
    return wires, archive_rows


def write_rows(path, rows):
    with path.open("x", encoding="utf8") as file:
        for row in rows:
            write_entry(file, row)


async def run(args):
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    rows, old = history()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.action == "check-history":
        cases, _ = register_cases(rows, fresh=False)
        print(
            json.dumps(
                {
                    "prior_ids": 1663,
                    "opening_cny": "3.00814688",
                    "inputs": len(cases),
                    "distinct_archive_wires": len(
                        {c["registered_wire"] for c in cases if c["mode"] == "archive"}
                    ),
                    "external_calls": 0,
                }
            )
        )
        return
    controls = control()
    cases, high = register_cases(rows)
    freeze_path = OUT / "freeze.json"
    archive_path = OUT / "archive.jsonl"
    outcomes_path = OUT / "outcomes.jsonl"
    if args.action == "freeze":
        if freeze_path.exists() or archive_path.exists():
            raise ValueError("Registration already exists")
        wires, archive_rows = await preflight(cases, high, old)
        for c in cases:
            c["registered_wire"] = wires[c["id"]]["request_sha256"]
        reg = dict(
            controls=controls,
            inputs=digest(cases),
            wires=wires,
            early_ids_sha=EARLY_HASH,
            previous_ledger_sha=TAIL_HASH,
            opening_units=CARRIED,
            archive_results=digest(archive_rows),
            archive_logical=len(archive_rows),
            live_pairs=sum(c["mode"] == "live" for c in cases),
        )
        persist(OUT / "cases.json", cases)
        write_rows(archive_path, archive_rows)
        persist(freeze_path, reg)
        print(
            json.dumps(
                {
                    "freeze_sha": digest(reg),
                    "archive_logical": len(archive_rows),
                    "live_pairs": reg["live_pairs"],
                    "external_calls": 0,
                }
            )
        )
        return
    reg = load(freeze_path)
    source = controls.pop("source")
    registered = dict(reg["controls"])
    ancestor = registered.pop("source")
    subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, source], cwd=ROOT, check=True)
    for c in cases:
        c["registered_wire"] = reg["wires"][c["id"]]["request_sha256"]
    if (
        digest(reg) != args.freeze_sha
        or controls != registered
        or load(OUT / "cases.json") != cases
        or digest(cases) != reg["inputs"]
        or reg["early_ids_sha"] != EARLY_HASH
        or reg["previous_ledger_sha"] != TAIL_HASH
    ):
        raise ValueError("Preflight, cases, account or executable hash changed")
    archive_rows = lines(archive_path)
    if digest(archive_rows) != reg["archive_results"]:
        raise ValueError("Registered archive result changed")
    live = [c for c in cases if c["mode"] == "live"]
    plan = [(c, arm) for i, c in enumerate(live) for arm in arm_order(i)]
    if args.action == "seal":
        outcomes = lines(outcomes_path)
        if outcomes[: len(archive_rows)] != archive_rows or [
            (o["id"], o["arm"]) for o in outcomes[len(archive_rows) :]
        ] != [(c["id"], a) for c, a in plan]:
            raise ValueError("Cannot close an incomplete campaign")
        by_pair = {}
        for r in outcomes:
            e = r["events"][0]
            if (
                e["request_sha256"] != reg["wires"][r["id"]]["request_sha256"]
                or by_pair.setdefault(r["id"], e["id"]) != e["id"]
            ):
                raise ValueError("First wire or paired provider response differs")
        report = account(outcomes, old, read_ledger(OUT / "ledger.sqlite"))
        report.update(freeze_sha=digest(reg), outcomes_sha=digest(outcomes))
        persist(OUT / "account.json", report)
        print(json.dumps(report))
        return
    # Exclusive intent is durable, even if the process crashes before the first append.
    # No automatic resume may replay ambiguous attempts.
    with (OUT / "dispatch.intent").open("x", encoding="ascii") as intent:
        intent.write(digest(reg))
        intent.flush()
        os.fsync(intent.fileno())
    if (OUT / "account.json").exists() or outcomes_path.exists():
        raise ValueError("Dispatch already started")
    meter = SuccessorMeter(OUT / "ledger.sqlite", old)
    if read_ledger(meter.path) != old:
        raise ValueError("Unexpected prior successor calls")
    transport = PairedTransport(old, meter)
    write_rows(outcomes_path, archive_rows)
    async with AsyncExitStack() as stack:
        endpoints = {
            a: await stack.enter_async_context(PairedEndpoint(aliyun_route(), a, transport))
            for a in ARMS
        }
        with outcomes_path.open("a", encoding="utf8") as file:
            for c, arm in plan:
                transport.select(c, None)
                out = await endpoints[arm].exchange(c)
                write_entry(file, out)
                print(c["id"], arm, out["http"], len(out["events"]), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check-history", "freeze", "dispatch", "seal"))
    parser.add_argument("--freeze-sha")
    asyncio.run(run(parser.parse_args()))
