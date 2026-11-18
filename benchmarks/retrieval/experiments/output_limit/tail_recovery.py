"""Exploratory 8192/16384 recovery with a sealed main account and separate results."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from benchmarks.retrieval.table_header_answers import write_entry
from scripts.aliyun_api_smoke import aliyun_route

from .campaign import DIRECTORY, ROOT, source_hashes
from .cases import digest
from .endpoint import ReplayEndpoint
from .grading import make_packets
from .meter import CAP, OPENING, Meter
from .report import read_ledger

TAIL = DIRECTORY / "tail"
MAIN_FREEZE = "e9edd73ebea75144837bb718e80eac23baa018afc1c2400a29a9a036fc5a8ed5"
CAPS = (8192, 16384)
PREFIX = "tail/"
ELIGIBILITY = (
    "Confirmed historical final HTTP 502 at the immediately preceding cap, "
    "final physical finish length, full completion cap, empty gateway body or "
    "strictly unparseable visible JSON."
)


def load(path):
    return json.loads(path.read_bytes())


def lines(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def persist(path, contents):
    if path.exists():
        if load(path) != contents:
            raise ValueError("Existing tail registration differs")
        return
    with path.open("x", encoding="utf8") as stream:
        json.dump(contents, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


class TailEndpoint(ReplayEndpoint):
    async def __aenter__(self):
        await super().__aenter__()
        try:
            # Clone the production-resolved 2048 profile, changing only its budget.
            with self.engine.begin() as db:
                for cap in CAPS:
                    ident = uuid4()
                    db.exec_driver_sql(
                        """INSERT INTO model_profiles
                        SELECT ?,workspace_id,provider_credential_id,model,temperature,?,
                        timeout_seconds,fallback_profile_id,capabilities,enabled,created_at
                        FROM model_profiles WHERE id=?""",
                        (ident.hex, cap, self.profiles[2048].hex),
                    )
                    self.profiles[cap] = ident
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise


def inputs():
    freeze = load(DIRECTORY / "freeze.json")
    cases = load(DIRECTORY / "cases.json")
    if (
        digest(freeze) != MAIN_FREEZE
        or digest(cases) != freeze["inputs"]
        or freeze["planned"] != 511
        or len(freeze["pairs"]) != 511
        or source_hashes() != freeze["source_sha"]
    ):
        raise RuntimeError("Main registration or execution code changed")
    return freeze, {c["id"]: c for c in cases if c["cohort"] == "historical"}


def tail_protocol():
    freeze, cases = inputs()
    if len(cases) != 57:
        raise ValueError("Missing historical population")
    return dict(
        main_freeze=MAIN_FREEZE,
        main_exchanges=511,
        historical_population=57,
        public_name="exploratory residual output-limit recovery",
        caps=list(CAPS),
        eligibility=ELIGIBILITY,
        route="same verified Beijing project route as main",
        main_input_hash=freeze["inputs"],
        code=hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        max_cumulative_cny="30.00000000",
        logical_limit="one exchange per qualifying original arm per cap, with product correction",
        promotion_gate=False,
        prior_observation=(
            "Two all-reasoning empty 2048 completions were observed "
            "before designing this exploration."
        ),
    )


def residual(row, cap):
    if row["http"] != 502 or row["cap"] != cap:
        return False
    event = row["events"][-1]
    if event["finish"] != "length" or event["usage"]["completion_tokens"] != cap:
        return False
    error = row["body"].get("error", {})
    text = event["content"] or ""
    if not text.strip():
        return (error.get("code"), error.get("message")) == (
            "MODEL_BAD_RESPONSE",
            "The model reached its output limit without visible content.",
        )
    if (
        error.get("code") != "CITATION_QA_INVALID_MODEL_OUTPUT"
        or "JSON parsing failed at the model output limit." not in error.get("message", "")
    ):
        return False
    from packages.knowledge.citation_qa import load_unique_json

    try:
        load_unique_json(text)
    except (ValueError, TypeError, RecursionError):
        return True
    return False


def reconcile(plan, outcomes, receipts, prefix=""):
    expected = [(r["id"], r["cap"], r["wire"]) for r in plan]
    observed = [(r["id"], r["cap"], r["events"][0]["request_sha256"]) for r in outcomes]
    if expected != observed:
        raise RuntimeError("Prior-stage outcomes incomplete or changed")
    matched = set()
    for r in outcomes:
        if not 1 <= len(r["events"]) <= 2:
            raise ValueError("Invalid physical count")
        for i, e in enumerate(r["events"], 1):
            receipt = receipts[e["id"]]
            b = json.loads(receipt["response"])
            choice = b["choices"][0]
            if (
                receipt["key"] != f"{prefix}{r['id']}/{r['cap']}/{i}"
                or receipt["cap"] != r["cap"]
                or receipt["digest"] != e["messages_sha256"]
                or receipt["cost"] is None
                or e["id"] in matched
                or (
                    b["id"],
                    b["usage"],
                    b["created"],
                    choice["finish_reason"],
                    choice["message"]["content"],
                )
                != (e["id"], e["usage"], e["created"], e["finish"], e["content"])
            ):
                raise ValueError("Prior-stage receipt differs from completed exchange")
            matched.add(e["id"])
    return matched


def main_seal():
    freeze, cases = inputs()
    prior = lines(DIRECTORY / "outcomes.jsonl")
    receipts = read_ledger(DIRECTORY / "ledger.sqlite")
    if reconcile(freeze["pairs"], prior, receipts) != set(receipts):
        raise ValueError("Main ledger contains unmatched receipts")
    return cases, prior, receipts


def attach_previous(main_receipts):
    """Exactly one writable successor ledger; the original completed main ledger stays sealed."""
    dest = TAIL / "ledger.sqlite"
    if not dest.exists():
        # Exclusive creation prevents two writers from bootstrapping separate budgets.
        with dest.open("xb"):
            pass
        with (
            closing(
                sqlite3.connect(
                    (DIRECTORY / "ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True
                )
            ) as src,
            closing(sqlite3.connect(dest)) as db,
        ):
            src.backup(db)
    receipts = read_ledger(dest)
    main_copy = {rid: r for rid, r in receipts.items() if not r["key"].startswith(PREFIX)}
    if main_copy != main_receipts:
        raise ValueError("Successor ledger does not contain the exact sealed main account")
    return receipts


def stage_paths(cap):
    return TAIL / f"freeze-{cap}.json", TAIL / f"outcomes-{cap}.jsonl"


async def dry_wires(cases, ids, cap):
    pairs = []
    async with TailEndpoint(aliyun_route()) as endpoint:
        for ident in ids:
            response, events = await endpoint.query(cases[ident], cap)
            if response.status_code != 200 or len(events) != 1:
                raise RuntimeError("Tail offline preflight failed")
            e = events[0]
            pairs.append(
                dict(
                    id=ident,
                    cap=cap,
                    wire=e["request_sha256"],
                    message=e["messages_sha256"],
                    params=e["parameters_sha256"],
                )
            )
    return pairs


def candidate_ids(cap, cases, main, receipts):
    if cap == 8192:
        previous = main
        prior_cap = 2048
    else:
        registration, path = stage_paths(8192)
        plan = load(registration)
        previous = lines(path) if path.exists() else []
        if (
            [p["id"] for p in plan["pairs"]] != candidate_ids(8192, cases, main, receipts)
            or plan["protocol"] != digest(tail_protocol())
            or plan["main_outcomes"] != digest(main)
        ):
            raise ValueError("Changed preceding stage registration")
        matches = reconcile(plan["pairs"], previous, receipts, PREFIX)
        expected_ids = {
            rid for rid, r in receipts.items() if r["key"].startswith(PREFIX) and r["cap"] == 8192
        }
        if matches != expected_ids:
            raise ValueError("Preceding stage contains unmatched receipts")
        prior_cap = 8192
    return [r["id"] for r in previous if r["id"] in cases and residual(r, prior_cap)]


async def run(action, cap, supplied):
    protocol = tail_protocol()
    TAIL.mkdir(parents=True, exist_ok=True)
    if action == "register":
        _, cases = inputs()
        # Initial registration may be run before main completes; inert local gateway only.
        sample = next(iter(cases))
        for maximum in CAPS:
            await dry_wires(cases, [sample], maximum)
        persist(TAIL / "protocol.json", protocol)
        print(json.dumps({"protocol": digest(protocol), "offline_caps": CAPS, "external_calls": 0}))
        return
    if load(TAIL / "protocol.json") != protocol:
        raise ValueError("Tail initial registration mismatch")
    cases, main, main_receipts = main_seal()
    archive = attach_previous(main_receipts)
    ids = candidate_ids(cap, cases, main, archive)
    pairs = await dry_wires(cases, ids, cap)
    data = dict(
        protocol=digest(protocol),
        main_outcomes=digest(main),
        main_ledger=digest(main_receipts),
        pairs=pairs,
        cap=cap,
    )
    reg, path = stage_paths(cap)
    if action == "freeze":
        persist(reg, data)
        print(
            json.dumps(
                {
                    "stage_sha": digest(data),
                    "eligible": len(ids),
                    "cap": cap,
                    "external_calls": 0,
                    "cumulative_cny": (OPENING + sum(r["cost"] for r in archive.values())) / 1e8,
                }
            )
        )
        return
    if load(reg) != data or supplied != digest(data):
        raise ValueError("Tail stage requires the matching explicit registered SHA")
    existing = lines(path) if path.exists() else []
    confirmed = reconcile(pairs[: len(existing)], existing, archive, PREFIX)
    stage_receipts = {
        rid for rid, r in archive.items() if r["key"].startswith(PREFIX) and r["cap"] == cap
    }
    if not confirmed <= stage_receipts:
        raise ValueError("Prior result outside current stage")
    if action == "status":
        if len(existing) != len(pairs) or confirmed != stage_receipts:
            raise RuntimeError("Incomplete tail stage")
        blind, keys = make_packets(list(cases.values()), existing)
        persist(TAIL / f"blind-{cap}.json", blind)
        persist(TAIL / f"grade-keys-{cap}.json", keys)
        grades_path = TAIL / f"grades-{cap}.json"
        summary = dict(
            cap=cap,
            logical=len(existing),
            http_200=sum(r["http"] == 200 for r in existing),
            still_length=sum(residual(r, cap) for r in existing),
            physical=len(confirmed),
            cost_cny=sum(archive[r]["cost"] for r in confirmed) / 1e8,
            semantic="pending",
        )
        if grades_path.exists():
            grades = load(grades_path)
            if sorted((g["id"], g["stage"], g["uid"], g["cap"]) for g in grades) != sorted(
                (k["id"], k["stage"], k["uid"], k["cap"]) for k in keys
            ):
                raise ValueError("Tail semantic grade coverage differs")
            summary["semantic"] = {
                stage: sum(g["strict"] for g in grades if g["stage"] == stage)
                for stage in ("first", "final")
            }
        print(json.dumps(summary))
        return
    # Verify product code before issuing any charged request, as in main dispatch.
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            "--quiet",
            inputs()[0]["code"],
            "--",
            "packages",
            "apps/api",
        ],
        cwd=ROOT,
        check=True,
    )
    meter = Meter(TAIL / "ledger.sqlite")
    async with TailEndpoint(aliyun_route(), meter=meter) as endpoint:
        with path.open("a", encoding="utf8") as out:
            for p in pairs[len(existing) :]:
                ident = p["id"]
                case = {**cases[ident], "id": PREFIX + ident, "registered_wire": p["wire"]}
                started = perf_counter()
                response, events = await endpoint.query(case, cap)
                write_entry(
                    out,
                    dict(
                        id=ident,
                        cap=cap,
                        http=response.status_code,
                        body=response.json(),
                        events=events,
                        elapsed_ms=(perf_counter() - started) * 1000,
                    ),
                )
                if response.status_code not in (200, 502):
                    raise RuntimeError("Unexpected tail endpoint status")
    print(json.dumps({"cap": cap, "complete": len(pairs), "budget_cny": CAP / 1e8}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "freeze", "dispatch", "status"))
    parser.add_argument("--cap", type=int, choices=CAPS, default=8192)
    parser.add_argument("--stage-sha")
    args = parser.parse_args()
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    asyncio.run(run(args.action, args.cap, args.stage_sha))
