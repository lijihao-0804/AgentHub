"""Paired original-evidence answers with exact-message reuse inside each question."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path

import httpx

from benchmarks.public_rag.consumer_answer_client import generate_once
from benchmarks.public_rag.consumer_answer_metrics import answer_metrics, citation_metrics
from benchmarks.public_rag.consumer_replay import compact
from benchmarks.public_rag.evidence_set_runner import PROFILES
from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_runner import dump


def request_groups(rows, profiles=PROFILES):
    """Compare actual messages, without content fingerprints or cross-question reuse."""
    jobs, reuse = [], {}
    for row in rows:
        seen = {}
        for profile in profiles:
            message = compact(row["arms"][profile]["messages"])
            pair = row["query_id"], profile
            if message in seen:
                reuse[pair] = seen[message]
            else:
                seen[message] = pair
                jobs.append((row, profile))
    return jobs, reuse


async def run(args):
    if args.output.exists():
        raise ValueError("refusing answer rerun or overwrite")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    meta = json.loads((args.prepared / "meta.json").read_text(encoding="utf-8"))
    done = json.loads((args.prepared / "completed.json").read_text(encoding="utf-8"))
    structure = json.loads((args.prepared / "structure.json").read_text(encoding="utf-8"))
    profiles = getattr(args, "profiles", None)
    profiles = profiles.split(",") if profiles else PROFILES
    if profiles not in (PROFILES, PROFILES[:2]):
        raise ValueError("only the fixed DEV or frozen primary comparison is supported")
    rows = [
        json.loads(s)
        for s in (args.prepared / "prepared.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if (
        meta["protocol"] != protocol
        or meta["profiles"] != PROFILES
        or not rows
        or not len(rows) == meta["queries"] == done["expected"] == done["queries"]
        or not structure["answer_stage_allowed"]
        or done["run_state"] != "COMPLETE"
        or len({r["query_id"] for r in rows}) != len(rows)
    ):
        raise ValueError("complete structure-accepted frozen preparation required")
    for row in rows:
        if not row["ordering_fragments_equal"]:
            raise ValueError("ordering diagnostic changed source fragments")
        for arm in row["arms"].values():
            budget = arm["budget"]
            if (
                not arm["admission_verified"]
                or budget["estimated_input_after"] > budget["available_input"]
            ):
                raise ValueError("invalid consumer admission")
    jobs, reuse = request_groups(rows, profiles)
    random.Random(protocol["seed"]).shuffle(jobs)
    first = next(j for j in jobs if j[1] == "legacy5")
    jobs.remove(first)
    jobs.insert(0, first)
    args.output.mkdir(parents=True)
    dump(
        args.output / "meta.json",
        {
            "protocol": protocol,
            "queries": len(rows),
            "profiles": profiles,
            "prepared": str(args.prepared),
            "dataset": "musique",
            "split": meta["split"],
            "reuse": "identical messages within the same question only",
            "planned_unique_calls": len(jobs),
            "planned_arm_outcomes": len(rows) * len(profiles),
        },
    )
    endpoint, key = _credentials()
    suffix = "/chat/completions"
    if not endpoint.endswith(suffix):
        raise ValueError("invalid endpoint shape")
    base = endpoint[: -len(suffix)]
    results, stop = {}, asyncio.Event()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)
    ) as client:
        with (args.output / "api-events.jsonl").open("x", encoding="utf-8") as events:
            iterator = iter(jobs)

            async def execute(row, profile):
                pair = row["query_id"], profile
                events.write(
                    compact({"event": "dispatch", "query_id": pair[0], "profile": profile}) + "\n"
                )
                events.flush()
                api = await generate_once(
                    row["arms"][profile]["messages"], client=client, base_url=base, api_key=key
                )
                results[pair] = api
                events.write(
                    compact(
                        {"event": "outcome", "query_id": pair[0], "profile": profile, "api": api}
                    )
                    + "\n"
                )
                events.flush()
                status = api["status"]
                if status in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"} or (
                    status == "PROVIDER_ERROR" and api.get("http_status") != 400
                ):
                    stop.set()
                if sum(r["status"] == "INVALID_OUTPUT" for r in results.values()) > max(
                    3, int(len(jobs) * 0.05)
                ):
                    stop.set()

            async def worker():
                while not stop.is_set():
                    job = next(iterator, None)
                    if job is None:
                        return
                    await execute(*job)

            await execute(*next(iterator))
            if results[(first[0]["query_id"], first[1])]["status"] != "VALID":
                stop.set()
            await asyncio.gather(*(worker() for _ in range(protocol["answers"]["workers"])))
    physical_calls = len(results)
    for pair, source in reuse.items():
        if source in results:
            results[pair] = {**results[source], "reused_from": list(source)}
    with (args.output / "run.jsonl").open("x", encoding="utf-8") as output:
        for row in rows:
            arms = {}
            for profile in profiles:
                arm = row["arms"][profile]
                api = results.get(
                    (row["query_id"], profile),
                    {"status": "NOT_ATTEMPTED", "usage": None, "request_ms": None},
                )
                valid = api["status"] == "VALID"
                evidence = arm["stages"]["final_admitted_context"]
                arms[profile] = {
                    "api": api,
                    **answer_metrics(api["answer"] if valid else "", row["answers"]),
                    "citation": citation_metrics(
                        api["cited_chunk_ids"] if valid else [], arm["admitted_chunk_ids"]
                    ),
                    "evidence_recall": evidence["best_evidence_recall"],
                    "complete_evidence_set": evidence["complete_evidence_set"],
                    "full_text_evidence_recall": evidence["full_text_evidence_recall"],
                    "final_evidence_bytes": arm["final_evidence_bytes"],
                }
            output.write(
                compact(
                    {
                        "query_id": row["query_id"],
                        "group_id": row["group_id"],
                        "hops": row["hops"],
                        "selection": row["selection"],
                        "arms": arms,
                    }
                )
                + "\n"
            )
    complete = not stop.is_set() and physical_calls == len(jobs)
    dump(
        args.output / "completed.json",
        {
            "expected": len(rows),
            "queries": len(rows),
            "planned_calls": len(jobs),
            "observed_calls": physical_calls,
            "logical_outcomes": len(results),
            "reused_outcomes": len(results) - physical_calls,
            "run_state": "COMPLETE" if complete else "NEEDS_ATTENTION",
        },
    )
    print(
        compact(
            {
                "run_state": "COMPLETE" if complete else "NEEDS_ATTENTION",
                "calls": physical_calls,
                "avoided_duplicate_calls": len(reuse),
            }
        )
    )
    if not complete:
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared", "protocol", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--profiles")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
