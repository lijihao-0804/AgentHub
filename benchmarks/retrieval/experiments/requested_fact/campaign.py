"""Freeze and complete independent, named and all historical requested-fact pairs."""

import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter, defaultdict
from contextlib import AsyncExitStack
from functools import lru_cache
from pathlib import Path

from benchmarks.retrieval.table_header_answers import write_entry
from scripts.aliyun_api_smoke import aliyun_route

from ..first_refusal_contract import campaign as primary
from ..first_refusal_contract.endpoint import ARMS, PairedTransport
from ..output_limit.cases import checked, digest, history_case_index, normalized
from ..output_limit.grading import project
from ..output_limit.meter import BLOCKED_MESSAGES, BLOCKED_QIDS
from ..output_limit.report import read_ledger
from ..output_limit.tail_recovery import lines, load, persist
from ..refusal_prompt import campaign as prev
from .billing import ACCOUNT, OPENING, FactMeter, close, opening
from .endpoint import DryTransport, FactEndpoint, arm_case, original

OUT = primary.ROOT / ".scratch/requested-fact-20261119"
FOLDER = Path(__file__).resolve().parent
STAGES = ("first", "final")
EARLY_DIR = primary.PREVIOUS


def source_control():
    ctrl = prev.source_control()
    folder = FOLDER.relative_to(primary.ROOT).as_posix()
    paths = [
        p
        for p in primary.git("ls-files", "--", folder).splitlines()
        if p.endswith((".py", "protocol.md", "fixtures/fresh.jsonl"))
    ]
    if any(
        p.endswith((".py", "protocol.md", "fixtures/fresh.jsonl"))
        for p in primary.git(
            "ls-files", "--others", "--exclude-standard", "--", folder
        ).splitlines()
    ) or set(primary.git("diff", "--name-only", "HEAD", "--", folder).splitlines()) & set(paths):
        raise ValueError("Requested-fact executable, fixture or protocol is uncommitted")
    ctrl["requested_fact_tree"] = digest(
        [
            s
            for s in primary.git("ls-tree", "-r", "HEAD", "--", folder).splitlines()
            if s.split("\t")[-1] in paths
        ]
    )
    return ctrl


def baseline_first(c, row):
    e = row["events"][0]
    valid, refused, answer, cited, reason = project(c, e["content"], e["finish"])
    rule = None
    if valid:
        rec = original.qa.resolve_citation_answer(
            c["question"],
            e["content"],
            visible_ids={s["id"] for s in c["selected"]},
            output_limit_reached=e["finish"] == "length",
        )
        rule = rec.rule
    return dict(valid=valid, refused=refused, answer=answer, cited=cited, reason=reason, rule=rule)


@lru_cache
def legacy_file(reference, name):
    content = subprocess.check_output(
        ["git", "show", reference + ":benchmarks/retrieval/experiments/" + name], cwd=primary.ROOT
    )
    return (
        [json.loads(line) for line in content.splitlines()]
        if name.endswith("jsonl")
        else json.loads(content)
    )


def prior_language_studies(main_cases):
    """Bind earlier 256-token judgments by original question and user evidence."""
    scratch = EARLY_DIR.parent
    bdata = "bilingual_refusal_100/results/"
    rdata = "first_refusal_100/results/"
    audit = {
        r["id"]: r
        for r in legacy_file("1041aef", rdata + "language-audit.json")["mismatching_questions"]
    }
    labels = defaultdict(dict)
    for era, ref, name in (
        ("R", "1041aef", rdata + "reviewed.jsonl"),
        ("B", "85563ae", bdata + "reviewed-rows.jsonl"),
    ):
        for row in legacy_file(ref, name):
            q, a = row["id"], row["arm"]
            for stage in STAGES:
                score = row[stage]["scores"]
                if era == "B":
                    value = score["semantic_and_citation_correct"] and score["language_match"]
                elif q in audit:
                    value = audit[q][a][stage]["pass_with_language"]
                else:
                    value = score["joint_correct"]
                labels[era, q, a][stage] = value
    for era, name in (("R-retest", "protected-100.json"), ("W-retest", "protected-wai14.json")):
        for r in legacy_file("85563ae", bdata + name)["rows"]:
            labels[era, r["id"], "candidate"][r["stage"]] = r["current"]
    by_user = {digest(c["messages"][1]): c for c in main_cases}
    by_qid = {c.get("qid", c["id"]): c for c in main_cases}
    waidir = scratch / "bilingual-refusal-20261118/wai14-live"
    raw = checked(
        waidir / "http-private.jsonl",
        "5898cd6e31e865f1c51ad3c80ecef960d7299ad701c36569080b350091b6dcd3",
    )
    wai = {
        r["key"]: r for line in raw.splitlines() if (r := json.loads(line))["state"] == "returned"
    }
    binding = json.loads(
        checked(
            waidir / "freeze.json",
            "0b476937dc69dbe1d33b1101f4c850c34324bd70415c9a0bf17346474d9b532b",
        )
    )["inputs"]["WAI"]
    out = []
    for era, folder, sha in (
        (
            "R",
            "first-refusal-100-20261118/independent",
            "e3f9cd2770805cdd2b9359aabd56e1fc4aca2e52d656c0841682ef27053d1394",
        ),
        (
            "B",
            "bilingual-refusal-20261118/independent",
            "2311bff003d0cd2f17186aec9a998e987793d5a58a1ec9525286743d7392c6c1",
        ),
        (
            "R-retest",
            "bilingual-refusal-20261118/old100-live",
            "a8f0a5a5bd8ba501397e5fa869eacf96a727d873be0dc04d5614b5d157f4f130",
        ),
        (
            "W-retest",
            "bilingual-refusal-20261118/wai14-live",
            "05e687df49f08f6a1fad5c76faed6566484f3d863114ce34963d66a2ba1dcffe",
        ),
    ):
        for line in checked(scratch / folder / "wire-private.jsonl", sha).splitlines():
            wire = json.loads(line)
            if wire["attempt"] != 1:
                continue
            qid, arm = wire["key"].split("/")
            flags = labels[era, qid, arm]
            if set(flags) != set(STAGES):
                raise ValueError("Early stage marks absent")
            if not any(flags.values()):
                continue
            user = wire["messages"][1]
            c = by_user.get(digest(user))
            if c is None:
                if era != "W-retest" or qid not in {"W06", "W07", "W12"}:
                    raise ValueError("Unmapped earlier success")
                record = wai[wire["key"]]
                trace = record["body"]["retrieval_trace"]["stages"]
                scores = {
                    k: {e["chunk_id"]: e["score"] for e in trace[k]["results"]}
                    for k in ("fused", "rerank")
                }
                source = f"document:{binding['document']}/revision:{binding['revision']}"
                selected = [
                    dict(
                        id=e["id"],
                        source_id=e["doc_id"],
                        source=source,
                        document=binding["document"],
                        revision=binding["revision"],
                        chunk_id=e["chunk_id"],
                        locator=e["locator"],
                        text=e["text"],
                        retrieval_score=scores["fused"][e["chunk_id"]],
                        rerank_score=scores["rerank"][e["chunk_id"]],
                    )
                    for e in record["evidence"]
                ]
                c = dict(
                    by_qid[qid],
                    messages=[main_cases[0]["messages"][0], user],
                    selected=selected,
                    kb=binding["kb"],
                    snapshot=binding["snapshot"],
                    qid=qid,
                )
                c.pop("first_request_sha256", None)
            out.append((c, dict(id=f"early/{era}/{qid}/{arm}", strict=flags, cap=256)))
    return out


def historical(first, second):
    one_cases, one_rows, one_grades = first
    two_cases, two_rows, two_grades = second
    old = load(EARLY_DIR / "cases.json")
    marked = lines(EARLY_DIR / "row-status.jsonl")
    if digest(marked) != "95fd8ca30a502301fb6fbf40926786498aa2967343f507a1f221e07ac4ee5c20":
        raise ValueError("Earlier stage marks changed")
    raw = lines(EARLY_DIR / "outcomes.jsonl")
    if digest(raw) != "26da46fe881dccab9f80002c149670c6ee1557e5a4b820b1e7c63d9e53cc5583":
        raise ValueError("Earlier exchanges changed")
    records = {}
    origins = defaultdict(list)
    baselines = defaultdict(list)
    roles = defaultdict(set)

    def keep(c, origin=None, role=None):
        key = prev.input_id(c)
        previous = records.setdefault(key, c)
        if previous["gold"] != c["gold"] or previous["question"] != c["question"]:
            raise ValueError("Conflicting source gold or question for one physical input")
        if origin is not None:
            origins[key].append(origin)
            if any(origin["strict"].values()):
                roles[key].add("protected")
        if role:
            roles[key].add(role)
        return key

    index = {c["id"]: dict(c, qid=c.get("qid", c["id"])) for c in old}
    for row in raw:
        if row["cap"] == 2048:
            c = index[row["id"]]
            key = keep(c)
            baselines[key].append(("output/" + c["id"], dict(row, first=baseline_first(c, row)), c))
    for r in marked:
        c = index[r["id"]]
        for source, flags in [
            ("output/" + str(r["cap"]), {s: r[s]["strict"] for s in STAGES}),
            ("earlier", r["historical_strict"]),
        ]:
            if source == "earlier" and r["cap"] != 2048:
                continue
            if any(flags.values()):
                keep(
                    c,
                    dict(
                        id=source + "/" + c["id"],
                        strict={s: flags[s] is True for s in STAGES},
                        cap=r["cap"] if source != "earlier" else 256,
                    ),
                )
    indexed = history_case_index(primary.ROOT)
    valid = 0
    for (group, qid, arm), c in indexed.items():
        flags = c["origins"][0]["stage_strict"]
        if any(flags.values()):
            keep(c, dict(id=f"joint-six/{group}/{qid}/{arm}", strict=flags, cap=256))
            valid += 1
    if len(indexed) != 556 or valid != 259:
        raise ValueError("Joint-six registration population changed")
    for c, mark in prior_language_studies(old):
        keep(c, mark)
    for era, cs, rs, gs in (
        ("one", one_cases, one_rows, one_grades),
        ("two", two_cases, two_rows, two_grades),
    ):
        idx = {c["id"]: c for c in cs}
        flags = {(g["id"], g["arm"], g["stage"]): g for g in gs}
        for r in rs:
            c = idx[r["id"]]
            key = keep(c)
            if r["arm"] == "baseline":
                baselines[key].append((era + "/" + c["id"], r, c))
            strict = {s: flags[c["id"], r["arm"], s]["strict"] for s in STAGES}
            if any(strict.values()):
                keep(c, dict(id=era + "/" + c["id"] + "/" + r["arm"], strict=strict, cap=2048))
            if c["cohort"] == "target":
                roles[key].add("named")
            if era == "two" and c["cohort"] == "fresh" and r["arm"] == "candidate":
                f = flags[c["id"], r["arm"], "first"]
                if not f["answerable"] and f["valid"] and not f["refused"]:
                    roles[key].add("false_release")
            if era == "one" and c["mode"] == "archive":
                roles[key].add("protected")
    history = {}
    cases = []
    # Earlier main 2048 replies are preferred in reverse chronological order.
    for key in sorted(k for k, v in roles.items() if v):
        c = records[key]
        chosen = baselines[key][-1] if baselines[key] else None
        if chosen:
            history[chosen[0]] = chosen[1]
            c = chosen[2]
        clean = {
            k: v
            for k, v in c.items()
            if k
            not in (
                "registered_wire",
                "arm_wires",
                "prior_strict",
                "origins",
                "named",
                "id",
                "mode",
                "cohort",
                "ref_id",
            )
        }
        cases.append(
            dict(
                clean,
                id="rf-history/" + key,
                mode="live",
                cohort="historical",
                ref_id=chosen[0] if chosen else None,
                message_sha256=digest(c["messages"]),
                named="named" in roles[key],
                false_release="false_release" in roles[key],
                protected="protected" in roles[key],
                gold_conflict=c["qid"] == "2hop__636562_141689",
                origins=origins[key],
                prior_strict={s: any(o["strict"][s] for o in origins[key]) for s in STAGES},
            )
        )
    if (
        sum(c["named"] for c in cases) != 7
        or sum(c["false_release"] for c in cases) != 17
        or sum(c["gold_conflict"] for c in cases) != 2
    ):
        raise ValueError("Named, false-release or conflict denominator changed")
    return cases, history


def independent(history_cases):
    rows = load(OUT / "independent/fresh-cases.json")
    fixture = {r["id"]: r for r in lines(FOLDER / "fixtures/fresh.jsonl")}
    if (
        len(rows) < 48
        or set(fixture) != {r["id"] for r in rows}
        or len(fixture) != len(rows)
        or min(
            Counter((r["language"], r["gold"]["answerable"]) for r in rows)[lang, a]
            for lang in ("en", "zh")
            for a in (False, True)
        )
        < 12
    ):
        raise ValueError("Independent registration incomplete")
    questions = [normalized(c["question"]) for c in rows]
    if len(set(questions)) != len(rows) or set(questions) & {
        normalized(c["question"]) for c in history_cases
    }:
        raise ValueError("Independent question overlap")
    output = []
    for c in rows:
        if (
            any(c[k] != v for k, v in fixture[c["id"]].items())
            or not c.get("kb")
            or not c.get("snapshot")
        ):
            raise ValueError("New question or retrieval binding drift")
        output.append(
            dict(
                c,
                id="rf-fresh/" + c["id"],
                qid=c["id"],
                mode="live",
                cohort="fresh",
                message_sha256=digest(c["messages"]),
                named=False,
                false_release=False,
                protected=False,
                gold_conflict=False,
                origins=[],
                prior_strict={s: False for s in STAGES},
                ref_id=None,
            )
        )
    return output


def schedule(cases):
    return [
        (c, a, "historical_baseline" if a == "baseline" and c["ref_id"] else "live")
        for i, c in enumerate(cases)
        for a in primary.arm_order(i)
    ]


def forecast(cases, history):
    plan = schedule(cases)
    paid = sum(src == "live" for _, _, src in plan)
    old_calls = [e for c in cases if c["ref_id"] for e in history[c["ref_id"]]["events"]]
    from ..first_refusal_contract.billing import charge
    from ..output_limit.report import money

    observed = sum(charge({"usage": e["usage"], "created": e["created"]}, 2048) for e in old_calls)
    historical_n = sum(bool(c["ref_id"]) for c in cases)
    # These older WAI reports retain questions and verdicts without complete
    # messages and original selected identities. Cover their questions, keeping
    # that weaker relationship out of same-input stage labels.
    early = json.loads(
        checked(
            EARLY_DIR.parent / "merge-qa-20261118/public-route-results.json",
            "625fba67e05b1b1fc21f14f201da95da6d8d934a3466d9e6cdcd0d89dc6239d0",
        )
    )
    queries = {
        normalized(r["query"])
        for arm in early["arms"].values()
        for r in arm
        if r["generated"]["joint"]
    }
    if len(queries) != 14 or queries - {normalized(c["question"]) for c in cases}:
        raise ValueError("Missing earlier WAI question")
    return dict(
        inputs=len(cases),
        historical=sum(c["cohort"] == "historical" for c in cases),
        fresh=sum(c["cohort"] == "fresh" for c in cases),
        named=sum(c["named"] for c in cases),
        false_release=sum(c["false_release"] for c in cases),
        protected=sum(c["protected"] for c in cases),
        prior_first_correct=sum(c["prior_strict"]["first"] for c in cases),
        prior_final_correct=sum(c["prior_strict"]["final"] for c in cases),
        new_first_calls=paid,
        max_new_physical=paid * 2,
        early_joint_six=dict(
            registered_arms=556,
            strict_arms=259,
            distinct_correct_inputs=len(
                {
                    prev.input_id(c)
                    for c in cases
                    if any(o["id"].startswith("joint-six/") for o in c["origins"])
                }
            ),
        ),
        conflicting_question_qid="2hop__636562_141689",
        conflicting_gold_note=(
            "Two different physical evidence inputs. The question asks for a family and "
            "the frozen Zoothera answer is a genus; retain both grades and report sensitivity."
        ),
        earliest_wai_question_only=dict(
            questions=14,
            covered=14,
            previous_256_joint_successes=20,
            same_input_certified=False,
        ),
        historical_same_input_baselines=historical_n,
        archived_model_invocations=len(old_calls),
        projected_cost_at_archived_average_cny=money(round(paid * observed / historical_n)),
        available_cny=money(3000000000 - OPENING),
        new_historical_baselines=[
            c["qid"] for c in cases if c["cohort"] == "historical" and not c["ref_id"]
        ],
    )


def check_one(obs, c, a, src, history):
    if (
        obs["id"] != c["id"]
        or obs["arm"] != a
        or obs["qid"] != c["qid"]
        or obs["cohort"] != c["cohort"]
        or obs["source"] != src
    ):
        raise ValueError("Outcome identity mismatch")
    first = obs["events"][0]
    if any(first[k] != v for k, v in c["arm_wires"][a].items()):
        raise ValueError("Physical wire differs from the source registration")
    if not 1 <= len(obs["events"]) <= 2 or obs["first"]["valid"] != first["parser"]["valid"]:
        raise ValueError("First attempt or correction count mismatch")
    if src == "historical_baseline":
        previous = history[c["ref_id"]]
        if (
            obs["mode"] != "archive"
            or not primary.historical_match(obs, previous)
            or obs["first"] != previous["first"]
            or any(e["provenance"] != "archived_receipt" for e in obs["events"])
        ):
            raise ValueError("Historical baseline does not match its sealed complete exchange")
    elif obs["mode"] != "live" or any(e["provenance"] != "provider" for e in obs["events"]):
        raise ValueError("Missing new provider completion")


async def preflight(cases, history, ledger):
    replay = PairedTransport(ledger)
    async with AsyncExitStack() as stack:
        eps = {a: await stack.enter_async_context(FactEndpoint(aliyun_route(), a)) for a in ARMS}
        for c in cases:
            c["arm_wires"] = {}
            for a in ARMS:
                src = "historical_baseline" if a == "baseline" and c["ref_id"] else "inert"
                local = dict(c, mode="archive" if src == "historical_baseline" else "live")
                ep = eps[a]
                if src == "historical_baseline":
                    replay.select(local, history[c["ref_id"]])
                    ep.meter = replay
                else:
                    ep.meter = DryTransport(a)
                obs = await ep.measure(local)
                e = obs["events"][0]
                c["arm_wires"][a] = {
                    k: e[k] for k in ("parameters_sha256", "request_sha256", "messages_sha256")
                }
                if (
                    e["messages_sha256"] != digest(arm_case(c, a)["messages"])
                    or e["messages_sha256"] in BLOCKED_MESSAGES
                    or c["qid"] in BLOCKED_QIDS
                ):
                    raise ValueError("Blocked input or incorrect prompt")
                if src == "historical_baseline":
                    check_one(dict(obs, source=src), c, a, src, history)
                elif (
                    obs["http"] != 200
                    or not obs["first"]["valid"]
                    or not obs["first"]["refused"]
                    or len(obs["events"]) != 1
                ):
                    raise ValueError("The registered contract did not accept its inert refusal")
            b, n = c["arm_wires"]["baseline"], c["arm_wires"]["candidate"]
            if (
                b["parameters_sha256"] != n["parameters_sha256"]
                or b["request_sha256"] == n["request_sha256"]
            ):
                raise ValueError("A/B differs outside the system contract or candidate is inactive")


def material():
    first, second, ledger = opening()
    cases, history = historical(first, second)
    new = independent([*cases, *first[0], *second[0], *load(EARLY_DIR / "cases.json")])
    if len({prev.input_id(c) for c in cases + new}) != len(cases) + len(new):
        raise ValueError("Repeated exact user/evidence input")
    return new + cases, history, ledger


def registered(directory):
    reg = load(directory / "freeze.json")
    cases = load(directory / "cases.json")
    expected, hist, ledger = material()
    stripped = [{k: v for k, v in c.items() if k != "arm_wires"} for c in cases]
    if (
        reg["previous_account"] != ACCOUNT
        or reg["inputs"] != digest(cases)
        or stripped != expected
        or reg["plan"] != forecast(cases, hist)
    ):
        raise ValueError("Source population, registration or prior account changed")
    return reg, cases, hist, ledger


def reconcile(directory):
    directory = Path(directory)
    reg, cases, hist, ledger = registered(directory)
    if load(directory / "dispatch.intent") != {"freeze_sha": digest(reg)}:
        raise ValueError("Unbound dispatch intent")
    rows = lines(directory / "outcomes.jsonl")
    plan = schedule(cases)
    if len(rows) != len(plan):
        raise ValueError("Independent or historical coverage incomplete")
    for r, (c, a, src) in zip(rows, plan, strict=True):
        check_one(r, c, a, src, hist)
    result = close(rows, ledger, read_ledger(directory / "ledger.sqlite"))
    if result["new_physical"] > reg["plan"]["max_new_physical"]:
        raise ValueError("Unexpected number of newly paid model responses")
    result["freeze_sha"] = digest(reg)
    if (directory / "account.json").exists() and load(directory / "account.json") != result:
        raise ValueError("Account seal differs from the independent receipt reconstruction")
    return result


async def run(args):
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    if args.action in ("plan", "freeze"):
        cases, history, ledger = material()
        plan = forecast(cases, history)
        if args.action == "plan":
            print(json.dumps(plan))
            return
        control = source_control()
        if (OUT / "freeze.json").exists():
            raise ValueError("Registration already present")
        await preflight(cases, history, ledger)
        reg = dict(previous_account=ACCOUNT, source=control, inputs=digest(cases), plan=plan)
        persist(OUT / "cases.json", cases)
        persist(OUT / "freeze.json", reg)
        print(json.dumps(dict(freeze_sha=digest(reg), external_calls=0, **plan)))
        return
    control = source_control()
    reg, cases, hist, ledger = registered(OUT)
    if args.freeze_sha != digest(reg) or any(
        control[k] != v for k, v in reg["source"].items() if k != "commit"
    ):
        raise ValueError("Changed executable source or wrong registration")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", reg["source"]["commit"], control["commit"]],
        cwd=primary.ROOT,
        check=True,
    )
    if args.action == "seal":
        report = reconcile(OUT)
        persist(OUT / "account.json", report)
        print(json.dumps(report))
        return
    # Persist intent before creating the successor ledger or sending a request.
    with (OUT / "dispatch.intent").open("x", encoding="ascii") as output:
        json.dump({"freeze_sha": digest(reg)}, output)
        output.flush()
        os.fsync(output.fileno())
    meter = FactMeter(OUT / "ledger.sqlite", ledger)
    transport = PairedTransport(ledger, meter)
    async with AsyncExitStack() as stack:
        eps = {
            a: await stack.enter_async_context(FactEndpoint(aliyun_route(), a, transport))
            for a in ARMS
        }
        with (OUT / "outcomes.jsonl").open("x", encoding="utf8") as output:
            for c, a, src in schedule(cases):
                local = dict(c, mode="archive" if src == "historical_baseline" else "live")
                transport.select(local, hist[c["ref_id"]] if src == "historical_baseline" else None)
                observed = await eps[a].measure(local, c["arm_wires"][a]["request_sha256"])
                observed["source"] = src
                check_one(observed, c, a, src, hist)
                write_entry(output, observed)
                print(c["id"], a, src, observed["http"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "freeze", "dispatch", "seal"))
    parser.add_argument("--freeze-sha")
    asyncio.run(run(parser.parse_args()))
