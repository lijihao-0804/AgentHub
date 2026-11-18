"""Freeze paired QA input hashes before any provider dispatch; keep all raw records local."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path
from time import perf_counter

from benchmarks.retrieval.table_header_answers import write_entry
from scripts.aliyun_api_smoke import aliyun_route

from .cases import digest, load_cases
from .endpoint import ReplayEndpoint, historical_failures
from .meter import Meter

ROOT = Path(__file__).resolve().parents[4]
DIRECTORY = ROOT / ".scratch/output-limit-20261119"
FOLDER = Path(__file__).parent
SOURCES = (
    "campaign.py",
    "cases.py",
    "endpoint.py",
    "fresh_retrieval.py",
    "meter.py",
    "grading.py",
    "protocol.md",
    "fixtures/fresh.jsonl",
    "fixtures/long-manifest.json",
    "fixtures/freeze_long.py",
)


def source_hashes():
    return {
        p: hashlib.sha256((FOLDER / p).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for p in SOURCES
    }


def schedule(cases):
    entries = []
    paired_index = 0
    for case in cases:
        if case["cohort"] in {"historical", "fresh", "fresh_long", "target"}:
            caps = (256, 2048) if paired_index % 2 == 0 else (2048, 256)
            paired_index += 1
        else:
            # These exact inputs already have signed archival baselines or historical
            # retained-success labels. Only the high cap is required for this guard.
            caps = (2048,)
        entries.extend((case["id"], cap) for cap in caps)
    return entries


def read_json(path):
    return json.loads(path.read_bytes())


def save(path, value):
    with path.open("x", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)


async def freeze():
    cases = historical_failures(ROOT) + sorted(
        load_cases(ROOT, DIRECTORY / "all-fresh-records.json"),
        key=lambda c: (
            {"fresh": 0, "fresh_long": 1, "target": 2, "protection": 3}[c["cohort"]],
            c["id"],
        ),
    )
    index = {c["id"]: c for c in cases}
    if len(index) != len(cases):
        raise ValueError("Duplicate case identity")
    order = schedule(cases)
    route = aliyun_route()
    hashes = []
    async with ReplayEndpoint(route) as endpoint:
        for key, cap in order:
            response, events = await endpoint.query(index[key], cap)
            if response.status_code != 200 or len(events) != 1:
                raise ValueError("Cannot freeze a different code path")
            first = events[0]
            hashes.append(
                dict(
                    id=key,
                    cap=cap,
                    message=first["messages_sha256"],
                    params=first["parameters_sha256"],
                    wire=first["request_sha256"],
                )
            )
    data = dict(
        source_sha=source_hashes(),
        code=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        inputs=digest(cases),
        counts=dict(Counter(c["cohort"] for c in cases)),
        planned=len(order),
        pairs=hashes,
    )
    save(DIRECTORY / "cases.json", cases)
    save(DIRECTORY / "freeze.json", data)
    print(json.dumps(dict(counts=data["counts"], planned=len(order), freeze_sha=digest(data))))


async def dispatch(supplied_hash):
    frozen = read_json(DIRECTORY / "freeze.json")
    cases = read_json(DIRECTORY / "cases.json")
    if (
        not supplied_hash
        or digest(frozen) != supplied_hash
        or digest(cases) != frozen["inputs"]
        or source_hashes() != frozen["source_sha"]
    ):
        raise ValueError("Campaign registration mismatch")
    subprocess.run(
        ["git", "diff", "--exit-code", "--quiet", frozen["code"], "--", "packages", "apps/api"],
        cwd=ROOT,
        check=True,
    )
    index = {c["id"]: c for c in cases}
    meter = Meter(DIRECTORY / "ledger.sqlite")
    path = DIRECTORY / "outcomes.jsonl"
    # A crashed append is an incomplete run. Reconstruct confirmed results locally
    # only after inspecting the durable ledger; never silently discard a partial line.
    existing = (
        [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []
    )
    if [(r["id"], r["cap"]) for r in existing] != [
        (p["id"], p["cap"]) for p in frozen["pairs"][: len(existing)]
    ]:
        raise RuntimeError("Outcome log differs from its registered prefix")
    async with ReplayEndpoint(aliyun_route(), meter=meter) as endpoint:
        with path.open("a", encoding="utf8") as output:
            for n, p in enumerate(frozen["pairs"][len(existing) :], len(existing) + 1):
                key, cap = p["id"], p["cap"]
                case = index[key]
                started = perf_counter()
                response, events = await endpoint.query({**case, "registered_wire": p["wire"]}, cap)
                elapsed = (perf_counter() - started) * 1000
                first = events[0]
                if (
                    first["request_sha256"],
                    first["messages_sha256"],
                    first["parameters_sha256"],
                ) != (p["wire"], p["message"], p["params"]):
                    raise ValueError("Unexpected dispatched payload")
                write_entry(
                    output,
                    dict(
                        id=key,
                        cap=cap,
                        http=response.status_code,
                        body=response.json(),
                        elapsed_ms=elapsed,
                        events=events,
                    ),
                )
                if response.status_code not in {200, 502}:
                    raise RuntimeError(f"Unexpected endpoint status {response.status_code}")
                if n == 1 or n % 20 == 0 or n == len(frozen["pairs"]):
                    print(n, len(frozen["pairs"]), case["cohort"], response.status_code, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "dispatch"))
    parser.add_argument("--freeze-sha")
    args = parser.parse_args()
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    asyncio.run(freeze() if args.action == "freeze" else dispatch(args.freeze_sha))
