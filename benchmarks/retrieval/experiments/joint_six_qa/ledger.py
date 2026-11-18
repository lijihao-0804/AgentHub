"""Freeze actual wire inputs and reconcile every raw reservation against its provider usage."""

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from benchmarks.retrieval.table_header_answers import receipt_cost
from scripts.aliyun_api_smoke import aliyun_route

from .inputs import (
    EMBED,
    FORMAL,
    FRESH,
    HISTORY_DIRS,
    OLD_FREEZE,
    OLD_KEY,
    OLD_LEDGER,
    RERANK,
    ROOT,
    STUDY,
    archive_guard,
    history_files,
    load,
    material,
    rows,
    save,
    sha,
)
from .wire import CAMPAIGN_CNY, CAP_CNY, MODEL, PRIOR_CONFIRMED, PRIOR_UNKNOWN, digest, reservation

MAIN = "4a0daa2ed29540ed0a0b46500c9a7e78671b55a6"
RUNTIME = ("inputs.py", "runner.py", "wire.py", "wire_check.py", "ledger.py")


def code(commit):
    paths = [Path(__file__).with_name(name) for name in RUNTIME]
    for directory in ("apps/api", "apps/worker", "packages"):
        paths.extend((ROOT / directory).rglob("*.py"))
    paths.extend(
        ROOT / p
        for p in (
            "benchmarks/retrieval/table_header_answers.py",
            "benchmarks/retrieval/table_header_local_qa.py",
            "benchmarks/retrieval/revision_reuse.py",
            "benchmarks/retrieval/runner.py",
            "scripts/aliyun_api_smoke.py",
            "benchmarks/public_rag/model-route.json",
            "benchmarks/retrieval/table_header_protocol.json",
        )
    )
    sources = {p.relative_to(ROOT).as_posix(): sha(p) for p in paths}
    # Git handles checkout line endings; hash the actual bytes as well.
    tracked = set(
        subprocess.check_output(["git", "ls-tree", "-r", "--name-only", commit], cwd=ROOT)
        .decode()
        .splitlines()
    )
    if not sources.keys() <= tracked:
        raise ValueError("Commit the measured application and QA runner before registration")
    result = subprocess.run(
        [
            "git",
            "diff",
            "--quiet",
            commit,
            "--",
            "apps/api",
            "apps/worker",
            "packages",
            *[p for p in sources if not p.startswith(("apps/", "packages/"))],
        ],
        cwd=ROOT,
    )
    if result.returncode:
        raise ValueError("The registered commit differs from the measured application")
    return sources


def weights():
    expected = load(OLD_FREEZE)["weights"]
    for kind, folder in (("embedding", EMBED), ("reranker", RERANK)):
        for filename, fingerprint in expected[kind].items():
            if sha(folder / filename) != fingerprint:
                raise ValueError(f"BGE identity differs for {kind}/{filename}")
    return expected


def check_selection(row):
    first, correction = row["events"]
    if row["http"] != 200 or first["messages"] != correction["messages"][:2]:
        raise ValueError("Offline product first response and correction preflight differ")
    input_ids = [r["chunk_id"] for r in row["rerank_input"]]
    ranked = {r["chunk_id"]: r for r in row["ranked"]}
    if not input_ids or len(input_ids) > 20 or set(input_ids) != ranked.keys():
        raise ValueError("Reranker pool differs from actually hydrated candidates")
    if not 1 <= len(row["selected"]) <= 6:
        raise ValueError("Evidence count differs from the six-item cap")
    for i, item in enumerate(row["selected"], 1):
        if item["id"] != i or dict(ranked[item["chunk_id"]], id=i) != item:
            raise ValueError("Selected citation differs from original reranked record")
    return first, correction


def prior_smoke():
    paid = STUDY / "paid-smoke"
    result = settle("smoke", require_complete=True)
    if load(paid / "billing.json") != result:
        raise ValueError("Smoke reconciliation differs from recorded result")
    return result


def registration(stage, commit):
    archive_guard()
    groups = ("smoke",) if stage == "smoke" else FORMAL
    preflight = STUDY / "preflight"
    plan, artifacts, controls = {}, {}, []
    registered_ids = set()
    for group in groups:
        bound = STUDY / "inputs" / group / "bindings.json"
        path = preflight / (group + ".jsonl")
        artifacts[group] = dict(bindings=sha(bound), offline=sha(path))
        cases = material(group)
        case_ids = {r["id"] for r in cases}
        if case_ids & registered_ids or OLD_KEY in case_ids:
            raise ValueError("Repeated or sealed case identity")
        registered_ids |= case_ids
        expected_ids = {
            (k, arm)
            for k in case_ids
            for arm in (("baseline",) if group == "smoke" else ("baseline", "candidate"))
        }
        observed = rows(path)
        if {(r["id"], r["arm"]) for r in observed} != expected_ids or len(observed) != len(
            expected_ids
        ):
            raise ValueError("Offline arm/denominator mismatch")
        baselines = {r["id"]: r for r in observed if r["arm"] == "baseline"}
        prior = {}
        if group in HISTORY_DIRS:
            state, _ = history_files(group)
            historical = state.parent / (group + ".jsonl")
            if (
                sha(historical)
                != load(OLD_FREEZE)["private_artifacts"][str(historical.relative_to(ROOT))]
            ):
                raise ValueError("Sealed previous zero-network control differs")
            prior = {r["id"]: r for r in rows(historical) if r["arm"] == "baseline"}
        for row in observed:
            first, correction = check_selection(row)
            key, baseline = row["id"] + "/" + row["arm"], baselines[row["id"]]
            if (
                baseline["rerank_input"] != row["rerank_input"]
                or baseline["ranked"] != row["ranked"]
                or baseline["deduped"] != row["deduped"]
            ):
                raise ValueError("A paired QA arm changed its retrieved/reranked candidate pool")
            if group in HISTORY_DIRS and row["arm"] == "baseline":
                controls.append(
                    dict(
                        id=row["id"],
                        current_default_equal_archived_default=prior[row["id"]]["events"][0][
                            "messages"
                        ]
                        == first["messages"],
                    )
                )
                continue
            first_bound = first["byte_bound"]
            # Recompute the exact actual bound again at raw transport before each physical call.
            # A preceding model reply expanding the correction by >4 KiB stops before dispatch.
            correction_bound = (
                0 if stage == "smoke" else min(32768 - 256, correction["byte_bound"] + 4096)
            )
            if first_bound > 32768 - 256 or (
                correction_bound and correction_bound < correction["byte_bound"]
            ):
                raise ValueError("Unbounded provider input")
            plan[key] = dict(
                group=group,
                first_bound=first_bound,
                correction_bound=correction_bound,
                message_sha=digest(first["messages"]),
                first_request_sha=first["request_sha256"],
                parameters_sha=first["parameters_sha256"],
            )
    smoke_cost = Decimal(0)
    if stage == "formal":
        smoke = prior_smoke()
        smoke_cost = Decimal(smoke["confirmed_cny"])
        artifacts["paid_smoke_ledger"] = smoke["ledger_sha256"]
        if any(k.split("/")[0] in {c["id"] for c in material("smoke")} for k in plan):
            raise ValueError("Smoke entered the primary sample")
    ceiling = sum(
        reservation(v["first_bound"])
        + (reservation(v["correction_bound"]) if v["correction_bound"] else 0)
        for v in plan.values()
    )
    if len(plan) != (1 if stage == "smoke" else 342) or ceiling + smoke_cost > CAMPAIGN_CNY:
        raise ValueError(
            "Registered denominator or worst-case peak reservation exceeds authorization"
        )
    return dict(
        schema=1,
        stage=stage,
        main=MAIN,
        code_commit=commit,
        sources=code(commit),
        model=MODEL,
        endpoint_sha256=digest(aliyun_route().base_url.rstrip("/")),
        weights=weights(),
        tariff=load(OLD_FREEZE)["tariff"],
        set_sha256=sha(FRESH),
        old_closed_ledger_sha256=sha(OLD_LEDGER),
        artifacts=artifacts,
        history_controls=controls,
        previous_confirmed_cny=str(PRIOR_CONFIRMED),
        previous_unresolved_reserves_cny=str(PRIOR_UNKNOWN),
        campaign_available_cny=str(CAMPAIGN_CNY),
        previous_smoke_cny=str(smoke_cost),
        cumulative_cap_cny=str(CAP_CNY),
        max_cny=str(ceiling),
        max_http=len(plan),
        max_wire=1 if stage == "smoke" else len(plan) * 2,
        plan=plan,
    )


def verify(stage, expected_sha, route):
    path = STUDY / (stage + "-freeze.json")
    if not expected_sha or sha(path) != expected_sha:
        raise ValueError("Frozen registration SHA required")
    frozen = load(path)
    if route.model != MODEL or digest(route.base_url.rstrip("/")) != frozen["endpoint_sha256"]:
        raise ValueError("Paid QA route differs from the frozen Beijing endpoint")
    if registration(stage, frozen["code_commit"]) != frozen:
        raise ValueError("Registration changed before dispatch")
    earlier = []
    if stage == "formal":
        earlier = [
            r["id"] for r in rows(STUDY / "paid-smoke/ledger.jsonl") if r["state"] == "confirmed"
        ]
    return frozen["plan"], Decimal(frozen["previous_smoke_cny"]), earlier


def settle(stage, *, require_complete):
    directory = STUDY / ("paid-" + stage)
    path = directory / "ledger.jsonl"
    freeze = load(STUDY / (stage + "-freeze.json"))
    plan = freeze["plan"]
    logical_events = {}
    for group in ("smoke",) if stage == "smoke" else FORMAL:
        logical = directory / (group + ".jsonl")
        if logical.is_file():
            for r in rows(logical):
                key = r["id"] + "/" + r["arm"]
                if key in logical_events or key not in plan:
                    raise ValueError("Duplicate or unregistered logical result")
                logical_events[key] = [e["id"] for e in r["events"]]
    events = rows(path)
    ids, by_key, totals, per_group = set(), defaultdict(list), Counter(), defaultdict(Counter)
    if stage == "formal":
        ids.update(
            r["id"] for r in rows(STUDY / "paid-smoke/ledger.jsonl") if r["state"] == "confirmed"
        )
    outstanding = Decimal(0)
    for i in range(0, len(events), 2):
        r = events[i]
        key, attempt = r["key"], r["attempt"]
        if (
            r["state"] != "reserved"
            or r["call"] != i // 2 + 1
            or key not in plan
            or attempt not in (1, 2)
            or attempt != len(by_key[key]) + 1
            or r["input_bound"] > plan[key]["first_bound" if attempt == 1 else "correction_bound"]
            or Decimal(r["cny"]) != reservation(r["input_bound"])
        ):
            raise ValueError("Reservation exceeds the registered physical attempt")
        totals["reserved"] += 1
        confirmed = events[i + 1] if i + 1 < len(events) else None
        if confirmed:
            if (confirmed["key"], confirmed["attempt"], confirmed["call"]) != (
                key,
                attempt,
                r["call"],
            ):
                raise ValueError("Settlement does not correspond to its reservation")
        if not confirmed or confirmed["state"] == "unknown":
            if i + 2 < len(events):
                raise ValueError("A follow-up reservation follows an unknown outcome")
            outstanding += Decimal(r["cny"])
            totals["unknown"] += 1
            break
        if (
            confirmed["state"] != "confirmed"
            or confirmed["id"] in ids
            or not 200 <= confirmed["status"] < 300
        ):
            raise ValueError("Missing or repeated provider receipt")
        raw = dict(
            id=confirmed["id"],
            created=confirmed["created"],
            model=confirmed["model"],
            usage=confirmed["usage"],
        )
        inp, out, cached, cost = receipt_cost(
            raw, MODEL, max_input=r["input_bound"], max_output=256
        )
        if (inp, out, cached, cost) != (
            confirmed["input"],
            confirmed["output"],
            confirmed["cached"],
            Decimal(confirmed["estimated_cny"]),
        ) or cost > Decimal(r["cny"]):
            raise ValueError("Recomputed tariff or token counters differ")
        ids.add(confirmed["id"])
        by_key[key].append(confirmed["id"])
        counts = dict(
            confirmed=1,
            input=inp,
            output=out,
            cached=cached,
            cny=cost,
            corrections=int(attempt == 2),
        )
        totals.update(counts)
        per_group[plan[key]["group"] + "/" + key.split("/")[-1]].update(counts)
    if any(by_key[k] != v for k, v in logical_events.items()):
        raise ValueError("Physical and logical receipt lists disagree")
    complete = (
        not totals["unknown"]
        and logical_events.keys() == plan.keys()
        and totals["reserved"] <= freeze["max_wire"]
    )
    if stage == "smoke" and complete:
        (smoke,) = rows(directory / "smoke.jsonl")
        complete = totals["confirmed"] == 1 and smoke["http"] == 200
    if require_complete and not complete:
        raise ValueError("Incomplete one-shot batch; do not resume or replay")

    def serial(counts):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in counts.items()}

    cumulative = (
        PRIOR_CONFIRMED
        + PRIOR_UNKNOWN
        + Decimal(freeze["previous_smoke_cny"])
        + totals["cny"]
        + outstanding
    )
    if cumulative > CAP_CNY:
        raise ValueError("Campaign cap exceeded")
    return dict(
        state="COMPLETE" if complete else "NEEDS_ATTENTION",
        reserved=totals["reserved"],
        confirmed=totals["confirmed"],
        unknown=totals["unknown"],
        corrections=totals["corrections"],
        confirmed_cny=str(totals["cny"]),
        outstanding_cny=str(outstanding),
        cumulative_with_reserves_cny=str(cumulative),
        unique_current_receipts=totals["confirmed"],
        input=totals["input"],
        output=totals["output"],
        cached=totals["cached"],
        logical_complete=len(logical_events),
        group_arm={k: serial(v) for k, v in sorted(per_group.items())},
        ledger_sha256=sha(path),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "reconcile"))
    parser.add_argument("stage", choices=("smoke", "formal"))
    args = parser.parse_args()
    if args.action == "freeze":
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        registration_path = STUDY / (args.stage + "-freeze.json")
        frozen = registration(args.stage, commit)
        save(registration_path, frozen)
        print(
            json.dumps(
                dict(
                    freeze_sha256=sha(registration_path),
                    code_commit=commit,
                    max_http=frozen["max_http"],
                    max_wire=frozen["max_wire"],
                    max_cny=frozen["max_cny"],
                )
            )
        )
    else:
        print(json.dumps(settle(args.stage, require_complete=False)))
